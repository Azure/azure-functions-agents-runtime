/**
 * Pure (vscode-free) front-matter location + YAML parsing with source offsets.
 * The extension glue converts the returned character offsets into vscode ranges.
 */
import { parseDocument, isNode, isPair, type Document } from "yaml";

export interface FrontMatter {
  /** YAML text between the fences (excludes the fence lines). */
  yamlText: string;
  /** Offset in the full document where the YAML content begins. */
  baseOffset: number;
  /** Parsed YAML document (may contain errors in doc.errors). */
  doc: Document.Parsed;
  /** Offset of the closing fence line start. */
  closeFenceStart: number;
}

/**
 * Locate the leading `---` ... `---` front matter block in markdown text.
 * Returns undefined if the document does not open with a front-matter fence.
 */
export function locateFrontMatter(text: string): FrontMatter | undefined {
  const bom = text.charCodeAt(0) === 0xfeff ? 1 : 0;
  const lines = text.slice(bom).split("\n");
  if (lines.length === 0) {
    return undefined;
  }
  if (lines[0].replace(/\r$/, "").trim() !== "---") {
    return undefined;
  }

  const lineStart: number[] = [];
  let offset = bom;
  for (let i = 0; i < lines.length; i++) {
    lineStart[i] = offset;
    offset += lines[i].length + 1; // account for the split '\n'
  }

  let close = -1;
  for (let i = 1; i < lines.length; i++) {
    const t = lines[i].replace(/\r$/, "").trim();
    if (t === "---" || t === "...") {
      close = i;
      break;
    }
  }
  if (close === -1) {
    return undefined;
  }

  const baseOffset = lineStart[1] ?? lineStart[0] + lines[0].length + 1;
  const closeFenceStart = lineStart[close];
  const yamlText = text.slice(baseOffset, closeFenceStart);
  const doc = parseDocument(yamlText, { keepSourceTokens: true });
  return { yamlText, baseOffset, doc, closeFenceStart };
}

/** Character offset range [start, end] for the value node at `path`, if resolvable. */
export function valueRangeForPath(
  fm: FrontMatter,
  path: Array<string | number>
): [number, number] | undefined {
  const node = path.length ? fm.doc.getIn(path, true) : fm.doc.contents;
  if (isNode(node) && node.range) {
    return [fm.baseOffset + node.range[0], fm.baseOffset + node.range[2]];
  }
  return undefined;
}

/** Character offset range for a specific key node inside the map at `parentPath`. */
export function keyRangeForPath(
  fm: FrontMatter,
  parentPath: Array<string | number>,
  key: string | number
): [number, number] | undefined {
  const parent = parentPath.length ? fm.doc.getIn(parentPath, true) : fm.doc.contents;
  const items = (parent as { items?: unknown[] })?.items;
  if (Array.isArray(items)) {
    for (const pair of items) {
      if (isPair(pair)) {
        const k = pair.key as { value?: unknown; range?: [number, number, number] };
        if (k && String(k.value) === String(key) && k.range) {
          return [fm.baseOffset + k.range[0], fm.baseOffset + k.range[2]];
        }
      }
    }
  }
  return undefined;
}

/** Top-level keys already present in the front matter (for completion filtering). */
export function topLevelKeys(fm: FrontMatter): string[] {
  const contents = fm.doc.contents as { items?: unknown[] } | null;
  const items = contents?.items;
  if (!Array.isArray(items)) {
    return [];
  }
  const keys: string[] = [];
  for (const pair of items) {
    if (isPair(pair)) {
      const k = pair.key as { value?: unknown };
      if (k && typeof k.value === "string") {
        keys.push(k.value);
      }
    }
  }
  return keys;
}
