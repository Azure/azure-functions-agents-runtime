/**
 * Diagnostics for `.agent.md` front matter and `agents.config.yaml`:
 * YAML parse errors, JSON-Schema violations (generated from the runtime models),
 * semantic checks, and identity-slug collisions.
 */
import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import type { ValidateFunction } from "ajv";
import {
  locateFrontMatter,
  valueRangeForPath,
  keyRangeForPath,
  type FrontMatter,
} from "./core/frontmatter";
import { createValidator, validateData, formatSchemaError, type SchemaError } from "./core/schemaValidate";
import { checkAgentSemantics } from "./core/semantics";
import { slugFromFilename } from "./core/slug";
import { isAgentDocument, isAgentsConfigDocument, findSlugCollisions } from "./workspace";
import { parseDocument } from "yaml";

function pointerToPath(instancePath: string): Array<string | number> {
  if (!instancePath) {
    return [];
  }
  return instancePath
    .split("/")
    .slice(1)
    .map((seg) => {
      const decoded = seg.replace(/~1/g, "/").replace(/~0/g, "~");
      return /^\d+$/.test(decoded) ? Number(decoded) : decoded;
    });
}

function offsetsToRange(document: vscode.TextDocument, offsets: [number, number]): vscode.Range {
  return new vscode.Range(document.positionAt(offsets[0]), document.positionAt(offsets[1]));
}

function fallbackRange(document: vscode.TextDocument, fm: FrontMatter): vscode.Range {
  const pos = document.positionAt(fm.baseOffset);
  return document.lineAt(pos.line).range;
}

function resolveRange(
  document: vscode.TextDocument,
  fm: FrontMatter,
  targetPath: Array<string | number>
): vscode.Range {
  for (let i = targetPath.length; i >= 1; i--) {
    const r = valueRangeForPath(fm, targetPath.slice(0, i));
    if (r) {
      return offsetsToRange(document, r);
    }
  }
  return fallbackRange(document, fm);
}

function rangeForSchemaError(document: vscode.TextDocument, fm: FrontMatter, err: SchemaError): vscode.Range {
  const parentPath = pointerToPath(err.instancePath);
  if (err.keyword === "additionalProperties") {
    const extra = (err.params as { additionalProperty?: string }).additionalProperty;
    const r = (extra !== undefined && keyRangeForPath(fm, parentPath, extra)) || valueRangeForPath(fm, parentPath);
    if (r) {
      return offsetsToRange(document, r);
    }
  }
  return resolveRange(document, fm, parentPath);
}

export class AgentDiagnostics {
  private readonly collection: vscode.DiagnosticCollection;
  private agentValidate?: ValidateFunction;
  private globalValidate?: ValidateFunction;

  constructor(private readonly extensionPath: string) {
    this.collection = vscode.languages.createDiagnosticCollection("agentsAuthoring");
  }

  private loadValidator(kind: "agent" | "global"): ValidateFunction {
    if (kind === "agent" && this.agentValidate) {
      return this.agentValidate;
    }
    if (kind === "global" && this.globalValidate) {
      return this.globalValidate;
    }
    const file = kind === "agent" ? "agent.schema.json" : "agents-config.schema.json";
    const schema = JSON.parse(fs.readFileSync(path.join(this.extensionPath, "schemas", file), "utf-8"));
    const validate = createValidator(schema);
    if (kind === "agent") {
      this.agentValidate = validate;
    } else {
      this.globalValidate = validate;
    }
    return validate;
  }

  private isEnabled(): boolean {
    return vscode.workspace.getConfiguration("agentsAuthoring").get<boolean>("validation.enabled", true);
  }

  async validate(document: vscode.TextDocument): Promise<void> {
    if (!this.isEnabled()) {
      this.collection.delete(document.uri);
      return;
    }
    if (isAgentDocument(document)) {
      await this.validateAgent(document);
    } else if (isAgentsConfigDocument(document)) {
      this.validateGlobal(document);
    }
  }

  private async validateAgent(document: vscode.TextDocument): Promise<void> {
    const diagnostics: vscode.Diagnostic[] = [];
    const text = document.getText();
    const fm = locateFrontMatter(text);

    if (!fm) {
      if (text.trim().length > 0) {
        diagnostics.push(
          new vscode.Diagnostic(
            document.lineAt(0).range,
            "Agent files must start with YAML front matter (--- ... ---) declaring at least `name` and `description`.",
            vscode.DiagnosticSeverity.Warning
          )
        );
      }
      this.collection.set(document.uri, diagnostics);
      return;
    }

    for (const err of fm.doc.errors) {
      const pos = err.pos;
      const range =
        pos && pos.length >= 2
          ? offsetsToRange(document, [fm.baseOffset + pos[0], fm.baseOffset + pos[1]])
          : fallbackRange(document, fm);
      diagnostics.push(new vscode.Diagnostic(range, `YAML: ${err.message}`, vscode.DiagnosticSeverity.Error));
    }

    if (fm.doc.errors.length === 0) {
      const data = fm.doc.toJS();
      const validate = this.loadValidator("agent");
      for (const err of validateData(validate, data).errors) {
        diagnostics.push(
          new vscode.Diagnostic(
            rangeForSchemaError(document, fm, err),
            formatSchemaError(err),
            vscode.DiagnosticSeverity.Error
          )
        );
      }
      for (const issue of checkAgentSemantics(data)) {
        diagnostics.push(
          new vscode.Diagnostic(
            resolveRange(document, fm, issue.path),
            issue.message,
            issue.severity === "error" ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning
          )
        );
      }

      const collisions = await findSlugCollisions(document.uri);
      if (collisions.length > 0) {
        const slug = slugFromFilename(path.basename(document.uri.fsPath));
        diagnostics.push(
          new vscode.Diagnostic(
            fallbackRange(document, fm),
            `Identity slug "${slug}" collides with: ${collisions.join(", ")}. Slugs must be globally unique across the app — rename one of the files.`,
            vscode.DiagnosticSeverity.Error
          )
        );
      }
    }

    this.collection.set(document.uri, diagnostics);
  }

  private validateGlobal(document: vscode.TextDocument): void {
    const diagnostics: vscode.Diagnostic[] = [];
    const text = document.getText();
    const doc = parseDocument(text, { keepSourceTokens: true });
    const fm: FrontMatter = { yamlText: text, baseOffset: 0, doc, closeFenceStart: text.length };

    for (const err of doc.errors) {
      const pos = err.pos;
      const range = pos && pos.length >= 2 ? offsetsToRange(document, [pos[0], pos[1]]) : document.lineAt(0).range;
      diagnostics.push(new vscode.Diagnostic(range, `YAML: ${err.message}`, vscode.DiagnosticSeverity.Error));
    }

    if (doc.errors.length === 0 && text.trim().length > 0) {
      const data = doc.toJS();
      if (data && typeof data === "object") {
        const validate = this.loadValidator("global");
        for (const err of validateData(validate, data).errors) {
          const parentPath = pointerToPath(err.instancePath);
          const r = valueRangeForPath(fm, parentPath);
          const range = r ? offsetsToRange(document, r) : document.lineAt(0).range;
          diagnostics.push(new vscode.Diagnostic(range, formatSchemaError(err), vscode.DiagnosticSeverity.Error));
        }
      }
    }

    this.collection.set(document.uri, diagnostics);
  }

  delete(uri: vscode.Uri): void {
    this.collection.delete(uri);
  }

  dispose(): void {
    this.collection.dispose();
  }
}
