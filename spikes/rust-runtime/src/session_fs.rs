//! Session filesystem provider (`SessionFsProvider`) backed by Azure Blob Storage or Local disk.
//!
//! Mirrors `src/azure_functions_agents/harness/copilot_sdk/_copilot_session_fs.py`,
//! `_copilot_session_blob.py`, `_copilot_session_local.py`, and `_copilot_session_paths.py`.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use chrono::{DateTime, Utc};
use github_copilot_sdk::session_fs::{
    DirEntry, DirEntryKind, FileInfo, FsError, FsErrorKind, SessionFsProvider,
};
use hmac::{Hmac, Mac};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use tokio::sync::Mutex;
use url::Url;

use crate::env::EnvMap;
use crate::skills::SkillDescriptor;

pub const DEFAULT_CONTAINER_NAME: &str = "azure-functions-agents";
pub const NAMESPACE: &str = "copilot-native";
pub const WORKSPACE_ROOT: &str = "/workspace";
pub const SESSION_STATE_ROOT: &str = "/session-state";

const AZURITE_ACCOUNT_NAME: &str = "devstoreaccount1";
const AZURITE_ACCOUNT_KEY: &str =
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==";
const AZURITE_BLOB_ENDPOINT: &str = "http://127.0.0.1:10000/devstoreaccount1";
const STORAGE_API_VERSION: &str = "2021-12-02";

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StorageMode {
    Local,
    Blob,
}

#[derive(Debug, Clone)]
pub enum BlobAuth {
    SharedKey {
        account_name: String,
        account_key: Vec<u8>,
    },
    BearerToken {
        token: Option<String>,
        client_id: Option<String>,
    },
}

#[derive(Debug, Clone)]
pub struct BlobStorageSettings {
    pub container_name: String,
    pub service_url: String,
    pub auth: BlobAuth,
}

#[derive(Debug, Clone)]
pub struct StorageRoute {
    pub local_dir: PathBuf,
    pub blob: Option<BlobStorageSettings>,
}

impl StorageRoute {
    pub fn mode(&self) -> StorageMode {
        if self.blob.is_some() {
            StorageMode::Blob
        } else {
            StorageMode::Local
        }
    }

    /// Resolve session storage settings from `EnvMap`.
    pub fn from_env(app_root: &Path, env: &EnvMap) -> Result<Self, FsError> {
        let local_dir = env
            .get_trimmed("AZURE_FUNCTIONS_AGENTS_LOCAL_STORAGE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| app_root.join(".azure-functions-agents"));

        let container_name = env
            .get_trimmed("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER")
            .unwrap_or(DEFAULT_CONTAINER_NAME)
            .to_string();

        if let Some(conn) = env.get_trimmed("AzureWebJobsStorage") {
            let settings = parse_connection_string(conn, &container_name)?;
            return Ok(Self {
                local_dir,
                blob: Some(settings),
            });
        }

        if let Some(service_uri) = env.get_trimmed("AzureWebJobsStorage__blobServiceUri") {
            let client_id = env
                .get_trimmed("AzureWebJobsStorage__clientId")
                .or_else(|| env.get_trimmed("AZURE_CLIENT_ID"))
                .map(ToString::to_string);
            let token = env
                .get_trimmed("AZURE_STORAGE_BEARER_TOKEN")
                .map(ToString::to_string);
            return Ok(Self {
                local_dir,
                blob: Some(BlobStorageSettings {
                    container_name,
                    service_url: service_uri.trim_end_matches('/').to_string(),
                    auth: BlobAuth::BearerToken { token, client_id },
                }),
            });
        }

        Ok(Self {
            local_dir,
            blob: None,
        })
    }
}

/// Parse an Azure Storage connection string (`UseDevelopmentStorage=true` or key-value pairs).
pub fn parse_connection_string(
    conn: &str,
    container_name: &str,
) -> Result<BlobStorageSettings, FsError> {
    let trimmed = conn.trim();
    if trimmed.eq_ignore_ascii_case("UseDevelopmentStorage=true")
        || trimmed.starts_with("UseDevelopmentStorage=true;")
    {
        let key_bytes = BASE64
            .decode(AZURITE_ACCOUNT_KEY)
            .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))?;
        return Ok(BlobStorageSettings {
            container_name: container_name.to_string(),
            service_url: AZURITE_BLOB_ENDPOINT.to_string(),
            auth: BlobAuth::SharedKey {
                account_name: AZURITE_ACCOUNT_NAME.to_string(),
                account_key: key_bytes,
            },
        });
    }

    let mut kv = HashMap::new();
    for part in trimmed.split(';') {
        if part.trim().is_empty() {
            continue;
        }
        if let Some((k, v)) = part.split_once('=') {
            kv.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }

    let account_name = kv.get("accountname").cloned().ok_or_else(|| {
        FsError::with_message(
            FsErrorKind::Other,
            "Connection string missing AccountName.",
        )
    })?;
    let account_key_b64 = kv.get("accountkey").ok_or_else(|| {
        FsError::with_message(FsErrorKind::Other, "Connection string missing AccountKey.")
    })?;
    let account_key = BASE64.decode(account_key_b64).map_err(|_| {
        FsError::with_message(
            FsErrorKind::Other,
            "Connection string AccountKey is not valid base64.",
        )
    })?;

    let service_url = if let Some(endpoint) = kv.get("blobendpoint") {
        endpoint.trim_end_matches('/').to_string()
    } else {
        let protocol = kv
            .get("defaultendpointsprotocol")
            .map(String::as_str)
            .unwrap_or("https");
        let suffix = kv
            .get("endpointsuffix")
            .map(String::as_str)
            .unwrap_or("core.windows.net");
        format!("{protocol}://{account_name}.blob.{suffix}")
    };

    Ok(BlobStorageSettings {
        container_name: container_name.to_string(),
        service_url,
        auth: BlobAuth::SharedKey {
            account_name,
            account_key,
        },
    })
}

/// Compute the canonical session prefix `copilot-native/{agent_slug}/{session_id}`.
pub fn session_prefix(agent_slug: &str, session_id: &str) -> Result<String, FsError> {
    for part in [agent_slug, session_id] {
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.contains('/')
            || part.contains('\\')
            || part.contains(':')
            || part.chars().any(|c| (c as u32) < 32)
        {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Invalid native session identity.",
            ));
        }
    }
    Ok(format!("{NAMESPACE}/{agent_slug}/{session_id}"))
}

/// Normalize and sandbox a callback path to `/workspace` or `/session-state`.
/// Returns the relative path without leading `/` (empty string `""` for `/`).
pub fn normalize_callback_path(path: &str, workspace_path: &str) -> Result<String, FsError> {
    if path.is_empty() || path.contains('\0') || path.contains('\\') {
        return Err(FsError::with_message(
            FsErrorKind::Other,
            "Invalid session path.",
        ));
    }

    let mut subject = path.to_string();
    let ws_prefix = workspace_path.trim_end_matches('/');
    if !ws_prefix.is_empty() && subject == ws_prefix {
        subject = WORKSPACE_ROOT.to_string();
    } else if !ws_prefix.is_empty() && subject.starts_with(&format!("{ws_prefix}/")) {
        subject = format!("{WORKSPACE_ROOT}/{}", &subject[ws_prefix.len() + 1..]);
    } else if path.starts_with("//")
        || (path.len() >= 2
            && path.as_bytes()[0].is_ascii_alphabetic()
            && path.as_bytes()[1] == b':')
    {
        return Err(FsError::with_message(
            FsErrorKind::Other,
            "Permission denied: host path outside session sandbox.",
        ));
    }

    if !subject.starts_with('/') {
        subject = format!("{WORKSPACE_ROOT}/{subject}");
    }

    let mut parts = Vec::new();
    for segment in subject.split('/') {
        if segment.is_empty() || segment == "." {
            continue;
        }
        if segment == ".." || segment.chars().any(|c| (c as u32) < 32) {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Permission denied: path traversal outside session sandbox.",
            ));
        }
        parts.push(segment);
    }

    let normalized = if parts.is_empty() {
        "/".to_string()
    } else {
        format!("/{}", parts.join("/"))
    };

    if normalized != "/"
        && !normalized.eq(WORKSPACE_ROOT)
        && !normalized.starts_with(&format!("{WORKSPACE_ROOT}/"))
        && !normalized.eq(SESSION_STATE_ROOT)
        && !normalized.starts_with(&format!("{SESSION_STATE_ROOT}/"))
    {
        return Err(FsError::with_message(
            FsErrorKind::Other,
            "Permission denied: path outside /workspace and /session-state.",
        ));
    }

    Ok(normalized.trim_start_matches('/').to_string())
}

#[async_trait]
pub trait SessionStorageBackend: Send + Sync {
    async fn initialize(&self) -> Result<(), FsError>;
    async fn stat(&self, rel_path: &str) -> Result<FileInfo, FsError>;
    async fn read_file(&self, rel_path: &str) -> Result<Vec<u8>, FsError>;
    async fn write_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError>;
    async fn append_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError>;
    async fn mkdir(&self, rel_path: &str, recursive: bool) -> Result<(), FsError>;
    async fn readdir_with_types(&self, rel_path: &str) -> Result<Vec<DirEntry>, FsError>;
    async fn rm(&self, rel_path: &str, recursive: bool, force: bool) -> Result<(), FsError>;
    async fn rename(&self, src_rel: &str, dest_rel: &str) -> Result<(), FsError>;
}

/// Local filesystem implementation of `SessionStorageBackend`.
pub struct LocalSessionBackend {
    root: PathBuf,
}

impl LocalSessionBackend {
    pub fn new(local_dir: &Path, prefix: &str) -> Self {
        Self {
            root: local_dir.join(prefix),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn resolve(&self, rel_path: &str) -> PathBuf {
        if rel_path.is_empty() {
            self.root.clone()
        } else {
            self.root.join(rel_path)
        }
    }
}

#[async_trait]
impl SessionStorageBackend for LocalSessionBackend {
    async fn initialize(&self) -> Result<(), FsError> {
        tokio::fs::create_dir_all(&self.root)
            .await
            .map_err(FsError::from)
    }

    async fn stat(&self, rel_path: &str) -> Result<FileInfo, FsError> {
        let target = self.resolve(rel_path);
        let meta = tokio::fs::metadata(&target).await.map_err(FsError::from)?;
        let mtime = meta
            .modified()
            .ok()
            .map(|t| DateTime::<Utc>::from(t).to_rfc3339())
            .unwrap_or_else(|| Utc::now().to_rfc3339());
        let birthtime = meta
            .created()
            .ok()
            .map(|t| DateTime::<Utc>::from(t).to_rfc3339())
            .unwrap_or_else(|| mtime.clone());
        Ok(FileInfo::new(
            meta.is_file(),
            meta.is_dir(),
            if meta.is_dir() { 0 } else { meta.len() as i64 },
            mtime,
            birthtime,
        ))
    }

    async fn read_file(&self, rel_path: &str) -> Result<Vec<u8>, FsError> {
        let target = self.resolve(rel_path);
        tokio::fs::read(&target).await.map_err(FsError::from)
    }

    async fn write_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError> {
        let target = self.resolve(rel_path);
        if let Some(parent) = target.parent() {
            tokio::fs::create_dir_all(parent)
                .await
                .map_err(FsError::from)?;
        }
        tokio::fs::write(&target, content)
            .await
            .map_err(FsError::from)
    }

    async fn append_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError> {
        use tokio::io::AsyncWriteExt;
        let target = self.resolve(rel_path);
        if let Some(parent) = target.parent() {
            tokio::fs::create_dir_all(parent)
                .await
                .map_err(FsError::from)?;
        }
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&target)
            .await
            .map_err(FsError::from)?;
        file.write_all(content).await.map_err(FsError::from)
    }

    async fn mkdir(&self, rel_path: &str, recursive: bool) -> Result<(), FsError> {
        let target = self.resolve(rel_path);
        if recursive {
            tokio::fs::create_dir_all(&target)
                .await
                .map_err(FsError::from)
        } else {
            tokio::fs::create_dir(&target).await.map_err(FsError::from)
        }
    }

    async fn readdir_with_types(&self, rel_path: &str) -> Result<Vec<DirEntry>, FsError> {
        let target = self.resolve(rel_path);
        let mut rd = tokio::fs::read_dir(&target).await.map_err(FsError::from)?;
        let mut entries = Vec::new();
        while let Some(entry) = rd.next_entry().await.map_err(FsError::from)? {
            let ft = entry.file_type().await.map_err(FsError::from)?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let kind = if ft.is_dir() {
                DirEntryKind::Directory
            } else {
                DirEntryKind::File
            };
            entries.push(DirEntry::new(name, kind));
        }
        entries.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(entries)
    }

    async fn rm(&self, rel_path: &str, recursive: bool, force: bool) -> Result<(), FsError> {
        if rel_path.is_empty() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Cannot remove session root.",
            ));
        }
        let target = self.resolve(rel_path);
        let meta = match tokio::fs::metadata(&target).await {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && force => return Ok(()),
            Err(e) => return Err(FsError::from(e)),
        };
        if meta.is_dir() {
            if recursive {
                tokio::fs::remove_dir_all(&target)
                    .await
                    .map_err(FsError::from)
            } else {
                tokio::fs::remove_dir(&target).await.map_err(FsError::from)
            }
        } else {
            tokio::fs::remove_file(&target).await.map_err(FsError::from)
        }
    }

    async fn rename(&self, src_rel: &str, dest_rel: &str) -> Result<(), FsError> {
        let src = self.resolve(src_rel);
        let dest = self.resolve(dest_rel);
        tokio::fs::rename(&src, &dest).await.map_err(FsError::from)
    }
}

/// Azure Blob Storage implementation of `SessionStorageBackend`.
///
/// Stores session files as block blobs under `{prefix}/{rel_path}` and directory
/// markers under `{prefix}/{rel_path}/` with `x-ms-meta-hdi_isfolder: true`,
/// matching `_copilot_session_blob.py`.
pub struct BlobSessionBackend {
    client: reqwest::Client,
    settings: BlobStorageSettings,
    prefix: String,
}

impl BlobSessionBackend {
    pub fn new(settings: BlobStorageSettings, prefix: &str) -> Self {
        Self {
            client: reqwest::Client::new(),
            settings,
            prefix: format!("{}/", prefix.trim_end_matches('/')),
        }
    }

    fn blob_name(&self, rel_path: &str) -> String {
        format!("{}{}", self.prefix, rel_path)
    }

    fn dir_marker_name(&self, rel_path: &str) -> String {
        if rel_path.is_empty() {
            self.prefix.clone()
        } else {
            format!("{}{}/", self.prefix, rel_path)
        }
    }

    fn blob_url(&self, blob_name: &str, query: Option<&str>) -> String {
        let encoded_blob: String = blob_name
            .split('/')
            .map(urlencoding_segment)
            .collect::<Vec<_>>()
            .join("/");
        let base = format!(
            "{}/{}/{}",
            self.settings.service_url.trim_end_matches('/'),
            self.settings.container_name,
            encoded_blob
        );
        match query {
            Some(q) if !q.is_empty() => format!("{base}?{q}"),
            _ => base,
        }
    }

    fn container_url(&self, query: &str) -> String {
        format!(
            "{}/{}?{}",
            self.settings.service_url.trim_end_matches('/'),
            self.settings.container_name,
            query
        )
    }

    async fn send_signed(
        &self,
        method: Method,
        url_str: &str,
        extra_headers: &[(&str, &str)],
        body: Option<&[u8]>,
    ) -> Result<reqwest::Response, FsError> {
        let parsed_url = Url::parse(url_str)
            .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))?;
        let now_http = Utc::now().format("%a, %d %b %Y %H:%M:%S GMT").to_string();
        let content_len = body.map(|b| b.len()).unwrap_or(0);

        let mut headers_map: BTreeMap<String, String> = BTreeMap::new();
        headers_map.insert("x-ms-date".to_string(), now_http.clone());
        headers_map.insert("x-ms-version".to_string(), STORAGE_API_VERSION.to_string());
        for (k, v) in extra_headers {
            headers_map.insert(k.to_ascii_lowercase(), (*v).to_string());
        }

        let auth_header = match &self.settings.auth {
            BlobAuth::SharedKey {
                account_name,
                account_key,
            } => {
                let sig = sign_shared_key(
                    method.as_str(),
                    content_len,
                    &headers_map,
                    &parsed_url,
                    account_name,
                    account_key,
                )?;
                format!("SharedKey {account_name}:{sig}")
            }
            BlobAuth::BearerToken { token, .. } => {
                let tok = token.as_deref().unwrap_or("anonymous");
                format!("Bearer {tok}")
            }
        };

        let mut req = self.client.request(method.clone(), parsed_url);
        for (k, v) in &headers_map {
            req = req.header(k, v);
        }
        req = req.header("Authorization", auth_header);
        if method == Method::PUT || body.is_some() {
            req = req.header("Content-Length", content_len.to_string());
        }
        if let Some(bytes) = body {
            req = req.body(bytes.to_vec());
        }

        req.send().await.map_err(|e| {
            FsError::with_message(
                FsErrorKind::Other,
                format!("Blob storage HTTP request failed: {e}"),
            )
        })
    }

    async fn head_blob(&self, blob_name: &str) -> Result<Option<FileInfo>, FsError> {
        let url = self.blob_url(blob_name, None);
        let resp = self.send_signed(Method::HEAD, &url, &[], None).await?;
        if resp.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        if !resp.status().is_success() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Blob HEAD failed with status {}", resp.status()),
            ));
        }
        let is_folder = resp
            .headers()
            .get("x-ms-meta-hdi_isfolder")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.eq_ignore_ascii_case("true"))
            || blob_name.ends_with('/');
        let size = if is_folder {
            0
        } else {
            resp.headers()
                .get("content-length")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<i64>().ok())
                .unwrap_or(0)
        };
        let mtime = resp
            .headers()
            .get("last-modified")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("1970-01-01T00:00:00Z")
            .to_string();
        let birthtime = resp
            .headers()
            .get("x-ms-creation-time")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(&mtime)
            .to_string();
        Ok(Some(FileInfo::new(
            !is_folder, is_folder, size, mtime, birthtime,
        )))
    }

    async fn list_blobs_with_prefix(&self, prefix: &str) -> Result<Vec<(String, i64)>, FsError> {
        let query = format!(
            "restype=container&comp=list&prefix={}",
            urlencoding_segment(prefix)
        );
        let url = self.container_url(&query);
        let resp = self.send_signed(Method::GET, &url, &[], None).await?;
        if !resp.status().is_success() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Blob list failed with status {}", resp.status()),
            ));
        }
        let xml = resp
            .text()
            .await
            .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))?;
        Ok(parse_blob_list_xml(&xml))
    }

    async fn lookup(&self, rel_path: &str) -> Result<Option<FileInfo>, FsError> {
        if !rel_path.is_empty()
            && let Some(info) = self.head_blob(&self.blob_name(rel_path)).await?
        {
            return Ok(Some(FileInfo::new(
                true,
                false,
                info.size,
                info.mtime,
                info.birthtime,
            )));
        }
        let dir_name = self.dir_marker_name(rel_path);
        if let Some(info) = self.head_blob(&dir_name).await? {
            return Ok(Some(FileInfo::new(
                false,
                true,
                0,
                info.mtime,
                info.birthtime,
            )));
        }
        let children = self.list_blobs_with_prefix(&dir_name).await?;
        if !children.is_empty() {
            let now = Utc::now().to_rfc3339();
            return Ok(Some(FileInfo::new(false, true, 0, now.clone(), now)));
        }
        Ok(None)
    }

    async fn upload_marker(&self, dir_rel: &str) -> Result<(), FsError> {
        let marker_name = self.dir_marker_name(dir_rel);
        let url = self.blob_url(&marker_name, None);
        let resp = self
            .send_signed(
                Method::PUT,
                &url,
                &[
                    ("x-ms-blob-type", "BlockBlob"),
                    ("x-ms-meta-hdi_isfolder", "true"),
                ],
                Some(b""),
            )
            .await?;
        if !resp.status().is_success() && resp.status() != StatusCode::CONFLICT {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Blob directory marker upload failed: {}", resp.status()),
            ));
        }
        Ok(())
    }
}

#[async_trait]
impl SessionStorageBackend for BlobSessionBackend {
    async fn initialize(&self) -> Result<(), FsError> {
        let url = self.container_url("restype=container");
        let resp = self.send_signed(Method::PUT, &url, &[], Some(b"")).await?;
        if !resp.status().is_success() && resp.status() != StatusCode::CONFLICT {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Failed to create/verify blob container: {}", resp.status()),
            ));
        }
        self.upload_marker("").await
    }

    async fn stat(&self, rel_path: &str) -> Result<FileInfo, FsError> {
        self.lookup(rel_path)
            .await?
            .ok_or_else(|| FsError::from(FsErrorKind::NotFound(rel_path.to_string())))
    }

    async fn read_file(&self, rel_path: &str) -> Result<Vec<u8>, FsError> {
        let info = self.stat(rel_path).await?;
        if info.is_directory {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Path is a directory.",
            ));
        }
        let url = self.blob_url(&self.blob_name(rel_path), None);
        let resp = self.send_signed(Method::GET, &url, &[], None).await?;
        if resp.status() == StatusCode::NOT_FOUND {
            return Err(FsError::from(FsErrorKind::NotFound(rel_path.to_string())));
        }
        if !resp.status().is_success() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Blob download failed: {}", resp.status()),
            ));
        }
        resp.bytes()
            .await
            .map(|b| b.to_vec())
            .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))
    }

    async fn write_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError> {
        if let Some((parent, _)) = rel_path.rsplit_once('/') {
            self.mkdir(parent, true).await?;
        }
        let url = self.blob_url(&self.blob_name(rel_path), None);
        let resp = self
            .send_signed(
                Method::PUT,
                &url,
                &[("x-ms-blob-type", "BlockBlob")],
                Some(content),
            )
            .await?;
        if !resp.status().is_success() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                format!("Blob write failed: {}", resp.status()),
            ));
        }
        Ok(())
    }

    async fn append_file(&self, rel_path: &str, content: &[u8]) -> Result<(), FsError> {
        let mut existing = match self.read_file(rel_path).await {
            Ok(bytes) => bytes,
            Err(err) if matches!(err.kind(), FsErrorKind::NotFound(_)) => Vec::new(),
            Err(err) => return Err(err),
        };
        existing.extend_from_slice(content);
        self.write_file(rel_path, &existing).await
    }

    async fn mkdir(&self, rel_path: &str, recursive: bool) -> Result<(), FsError> {
        if let Some(info) = self.lookup(rel_path).await? {
            if recursive && info.is_directory {
                return Ok(());
            }
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Path already exists.",
            ));
        }
        if !rel_path.is_empty() {
            let parent = rel_path.rsplit_once('/').map(|(p, _)| p).unwrap_or("");
            if recursive {
                Box::pin(self.mkdir(parent, true)).await?;
            }
        }
        self.upload_marker(rel_path).await
    }

    async fn readdir_with_types(&self, rel_path: &str) -> Result<Vec<DirEntry>, FsError> {
        let info = self.stat(rel_path).await?;
        if !info.is_directory {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Not a directory.",
            ));
        }
        let dir_prefix = self.dir_marker_name(rel_path);
        let blobs = self.list_blobs_with_prefix(&dir_prefix).await?;
        let mut entries: BTreeMap<String, DirEntryKind> = BTreeMap::new();
        for (name, _) in blobs {
            let Some(relative) = name.strip_prefix(&dir_prefix) else {
                continue;
            };
            if relative.is_empty() {
                continue;
            }
            if let Some((child_name, _rest)) = relative.split_once('/') {
                entries.insert(child_name.to_string(), DirEntryKind::Directory);
            } else {
                entries
                    .entry(relative.to_string())
                    .or_insert(DirEntryKind::File);
            }
        }
        Ok(entries
            .into_iter()
            .map(|(name, kind)| DirEntry::new(name, kind))
            .collect())
    }

    async fn rm(&self, rel_path: &str, recursive: bool, force: bool) -> Result<(), FsError> {
        if rel_path.is_empty() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Cannot remove session root.",
            ));
        }
        let info = match self.stat(rel_path).await {
            Ok(i) => i,
            Err(e) if matches!(e.kind(), FsErrorKind::NotFound(_)) && force => return Ok(()),
            Err(e) => return Err(e),
        };
        if info.is_file {
            let url = self.blob_url(&self.blob_name(rel_path), None);
            let resp = self.send_signed(Method::DELETE, &url, &[], None).await?;
            if !resp.status().is_success() && resp.status() != StatusCode::NOT_FOUND {
                return Err(FsError::with_message(
                    FsErrorKind::Other,
                    format!("Blob delete failed: {}", resp.status()),
                ));
            }
        } else {
            if !recursive && !self.readdir_with_types(rel_path).await?.is_empty() {
                return Err(FsError::with_message(
                    FsErrorKind::Other,
                    "Directory not empty.",
                ));
            }
            let dir_prefix = self.dir_marker_name(rel_path);
            for (blob_name, _) in self.list_blobs_with_prefix(&dir_prefix).await? {
                let url = self.blob_url(&blob_name, None);
                let _ = self.send_signed(Method::DELETE, &url, &[], None).await?;
            }
        }
        Ok(())
    }

    async fn rename(&self, src_rel: &str, dest_rel: &str) -> Result<(), FsError> {
        let info = self.stat(src_rel).await?;
        if info.is_file {
            let bytes = self.read_file(src_rel).await?;
            self.write_file(dest_rel, &bytes).await?;
            self.rm(src_rel, false, true).await?;
        } else {
            self.mkdir(dest_rel, true).await?;
            let src_prefix = self.dir_marker_name(src_rel);
            for (blob_name, _) in self.list_blobs_with_prefix(&src_prefix).await? {
                let Some(suffix) = blob_name.strip_prefix(&src_prefix) else {
                    continue;
                };
                if suffix.is_empty() || suffix.ends_with('/') {
                    continue;
                }
                let child_src = format!("{src_rel}/{suffix}");
                let child_dest = format!("{dest_rel}/{suffix}");
                let bytes = self.read_file(&child_src).await?;
                self.write_file(&child_dest, &bytes).await?;
            }
            self.rm(src_rel, true, true).await?;
        }
        Ok(())
    }
}

/// Session-scoped `SessionFsProvider` implementation with mutex serialization
/// and read-only approved skill file pass-through.
pub struct CopilotSessionFs {
    backend: Arc<dyn SessionStorageBackend>,
    workspace_path: String,
    approved_skill_roots: Vec<PathBuf>,
    guard: Mutex<()>,
}

impl CopilotSessionFs {
    pub async fn open(
        route: &StorageRoute,
        agent_slug: &str,
        session_id: &str,
        workspace_path: impl Into<String>,
        approved_skills: &[SkillDescriptor],
    ) -> Result<Arc<Self>, FsError> {
        let prefix = session_prefix(agent_slug, session_id)?;
        let backend: Arc<dyn SessionStorageBackend> = match &route.blob {
            Some(blob_settings) => Arc::new(BlobSessionBackend::new(blob_settings.clone(), &prefix)),
            None => Arc::new(LocalSessionBackend::new(&route.local_dir, &prefix)),
        };
        backend.initialize().await?;
        backend
            .mkdir(WORKSPACE_ROOT.trim_start_matches('/'), true)
            .await?;
        backend
            .mkdir(SESSION_STATE_ROOT.trim_start_matches('/'), true)
            .await?;

        Ok(Arc::new(Self {
            backend,
            workspace_path: workspace_path.into(),
            approved_skill_roots: approved_skills.iter().map(|s| s.path.clone()).collect(),
            guard: Mutex::new(()),
        }))
    }

    fn resolve_skill_read(&self, path: &str) -> Option<PathBuf> {
        let candidate = Path::new(path);
        if !candidate.is_absolute() {
            return None;
        }
        let canonical = candidate.canonicalize().ok()?;
        for root in &self.approved_skill_roots {
            if canonical == *root || canonical.starts_with(root) {
                return Some(canonical);
            }
        }
        None
    }

    fn normalize(&self, path: &str) -> Result<String, FsError> {
        if self.resolve_skill_read(path).is_some() {
            return Err(FsError::with_message(
                FsErrorKind::Other,
                "Approved skill files are read-only.",
            ));
        }
        normalize_callback_path(path, &self.workspace_path)
    }

    /// Check whether this session namespace already has persisted state beyond empty scaffolding.
    pub async fn has_persisted_state(&self) -> Result<bool, FsError> {
        let _lock = self.guard.lock().await;
        let state_entries = self
            .backend
            .readdir_with_types(SESSION_STATE_ROOT.trim_start_matches('/'))
            .await
            .unwrap_or_default();
        if !state_entries.is_empty() {
            return Ok(true);
        }
        let ws_entries = self
            .backend
            .readdir_with_types(WORKSPACE_ROOT.trim_start_matches('/'))
            .await
            .unwrap_or_default();
        Ok(!ws_entries.is_empty())
    }
}

#[async_trait]
impl SessionFsProvider for CopilotSessionFs {
    async fn read_file(&self, path: &str) -> Result<String, FsError> {
        let _lock = self.guard.lock().await;
        if let Some(skill_path) = self.resolve_skill_read(path) {
            return tokio::fs::read_to_string(skill_path)
                .await
                .map_err(FsError::from);
        }
        let rel = self.normalize(path)?;
        let bytes = self.backend.read_file(&rel).await?;
        String::from_utf8(bytes)
            .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))
    }

    async fn write_file(
        &self,
        path: &str,
        content: &str,
        _mode: Option<i64>,
    ) -> Result<(), FsError> {
        let _lock = self.guard.lock().await;
        let rel = self.normalize(path)?;
        self.backend.write_file(&rel, content.as_bytes()).await
    }

    async fn append_file(
        &self,
        path: &str,
        content: &str,
        _mode: Option<i64>,
    ) -> Result<(), FsError> {
        let _lock = self.guard.lock().await;
        let rel = self.normalize(path)?;
        self.backend.append_file(&rel, content.as_bytes()).await
    }

    async fn exists(&self, path: &str) -> Result<bool, FsError> {
        match self.stat(path).await {
            Ok(_) => Ok(true),
            Err(e) if matches!(e.kind(), FsErrorKind::NotFound(_)) => Ok(false),
            Err(e) => Err(e),
        }
    }

    async fn stat(&self, path: &str) -> Result<FileInfo, FsError> {
        let _lock = self.guard.lock().await;
        if let Some(skill_path) = self.resolve_skill_read(path) {
            let meta = tokio::fs::metadata(&skill_path)
                .await
                .map_err(FsError::from)?;
            let now = Utc::now().to_rfc3339();
            return Ok(FileInfo::new(
                meta.is_file(),
                meta.is_dir(),
                meta.len() as i64,
                now.clone(),
                now,
            ));
        }
        let rel = self.normalize(path)?;
        self.backend.stat(&rel).await
    }

    async fn mkdir(&self, path: &str, recursive: bool, _mode: Option<i64>) -> Result<(), FsError> {
        let _lock = self.guard.lock().await;
        let rel = self.normalize(path)?;
        self.backend.mkdir(&rel, recursive).await
    }

    async fn readdir(&self, path: &str) -> Result<Vec<String>, FsError> {
        let entries = self.readdir_with_types(path).await?;
        Ok(entries.into_iter().map(|e| e.name).collect())
    }

    async fn readdir_with_types(&self, path: &str) -> Result<Vec<DirEntry>, FsError> {
        let _lock = self.guard.lock().await;
        let rel = self.normalize(path)?;
        self.backend.readdir_with_types(&rel).await
    }

    async fn rm(&self, path: &str, recursive: bool, force: bool) -> Result<(), FsError> {
        let _lock = self.guard.lock().await;
        let rel = self.normalize(path)?;
        self.backend.rm(&rel, recursive, force).await
    }

    async fn rename(&self, src: &str, dest: &str) -> Result<(), FsError> {
        let _lock = self.guard.lock().await;
        let src_rel = self.normalize(src)?;
        let dest_rel = self.normalize(dest)?;
        self.backend.rename(&src_rel, &dest_rel).await
    }
}

fn urlencoding_segment(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for b in input.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn sign_shared_key(
    method: &str,
    content_len: usize,
    headers: &BTreeMap<String, String>,
    url: &Url,
    account_name: &str,
    account_key: &[u8],
) -> Result<String, FsError> {
    let cl_str = if content_len == 0 {
        String::new()
    } else {
        content_len.to_string()
    };

    let mut canonicalized_headers = String::new();
    for (k, v) in headers {
        if k.starts_with("x-ms-") {
            canonicalized_headers.push_str(&format!("{}:{}\n", k.trim(), v.trim()));
        }
    }

    let mut canonicalized_resource = format!("/{account_name}{}", url.path());
    let mut query_pairs: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (k, v) in url.query_pairs() {
        query_pairs
            .entry(k.to_ascii_lowercase())
            .or_default()
            .push(v.into_owned());
    }
    for (k, mut vals) in query_pairs {
        vals.sort();
        canonicalized_resource.push_str(&format!("\n{k}:{}", vals.join(",")));
    }

    // StringToSign for Blob service 2015-02-21 and later:
    let string_to_sign = format!(
        "{method}\n\n\n{cl_str}\n\n\n\n\n\n\n\n\n{canonicalized_headers}{canonicalized_resource}"
    );

    let mut mac = HmacSha256::new_from_slice(account_key)
        .map_err(|e| FsError::with_message(FsErrorKind::Other, e.to_string()))?;
    mac.update(string_to_sign.as_bytes());
    Ok(BASE64.encode(mac.finalize().into_bytes()))
}

fn parse_blob_list_xml(xml: &str) -> Vec<(String, i64)> {
    let mut items = Vec::new();
    for block in xml.split("<Blob>").skip(1) {
        let Some(end_idx) = block.find("</Blob>") else {
            continue;
        };
        let blob_xml = &block[..end_idx];
        let Some(name) = extract_xml_tag(blob_xml, "Name") else {
            continue;
        };
        let len = extract_xml_tag(blob_xml, "Content-Length")
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(0);
        items.push((name.to_string(), len));
    }
    items
}

fn extract_xml_tag<'a>(xml: &'a str, tag: &str) -> Option<&'a str> {
    let open = format!("<{tag}>");
    let close = format!("</{tag}>");
    let start = xml.find(&open)? + open.len();
    let rest = &xml[start..];
    let end = rest.find(&close)?;
    Some(&rest[..end])
}

#[cfg(test)]
mod tests {
    use tempfile::TempDir;

    use super::*;

    #[tokio::test]
    async fn local_session_fs_roundtrip_and_path_sandboxing() {
        let tmp = TempDir::new().unwrap();
        let route = StorageRoute {
            local_dir: tmp.path().to_path_buf(),
            blob: None,
        };
        let fs = CopilotSessionFs::open(&route, "reviewer", "sess-123", "/host/workspace", &[])
            .await
            .unwrap();

        assert!(!fs.has_persisted_state().await.unwrap());

        fs.write_file("/session-state/history.json", "{\"turn\":1}", None)
            .await
            .unwrap();
        fs.append_file("/workspace/notes.txt", "line1\n", None)
            .await
            .unwrap();
        fs.append_file("/host/workspace/notes.txt", "line2\n", None)
            .await
            .unwrap();

        assert!(fs.has_persisted_state().await.unwrap());
        assert_eq!(
            fs.read_file("/session-state/history.json").await.unwrap(),
            "{\"turn\":1}"
        );
        assert_eq!(
            fs.read_file("/workspace/notes.txt").await.unwrap(),
            "line1\nline2\n"
        );

        // Path traversal outside /workspace or /session-state is rejected.
        assert!(fs.read_file("/etc/passwd").await.is_err());
        assert!(fs.read_file("/workspace/../../etc/passwd").await.is_err());
    }

    #[test]
    fn parses_azurite_and_custom_blob_connection_strings() {
        let azurite = parse_connection_string("UseDevelopmentStorage=true", "my-container").unwrap();
        assert_eq!(azurite.container_name, "my-container");
        assert_eq!(azurite.service_url, AZURITE_BLOB_ENDPOINT);

        let custom = parse_connection_string(
            "DefaultEndpointsProtocol=http;AccountName=testacc;AccountKey=c2VjcmV0;BlobEndpoint=http://127.0.0.1:19000/testacc;",
            "agents",
        )
        .unwrap();
        assert_eq!(custom.service_url, "http://127.0.0.1:19000/testacc");
    }
}
