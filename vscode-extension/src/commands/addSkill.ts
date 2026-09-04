/**
 * "Add Skill" command — scaffolds `skills/<name>/SKILL.md`.
 */
import * as vscode from "vscode";
import { buildSkillMarkdown, toSkillSlug } from "../core/skillTemplate";
import { resolveAppRoot } from "../workspace";

export async function addSkillCommand(): Promise<void> {
  const name = await vscode.window.showInputBox({
    title: "Add Skill — name",
    prompt: "Skill name (becomes the folder name, e.g. azure-resources)",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Name is required" : undefined),
  });
  if (name === undefined) {
    return;
  }

  const description = await vscode.window.showInputBox({
    title: "Add Skill — description",
    prompt: "Describe when the agent should use this skill (used for skill selection)",
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() === "" ? "Description is required" : undefined),
  });
  if (description === undefined) {
    return;
  }

  const appRoot = await resolveAppRoot();
  if (!appRoot) {
    vscode.window.showErrorMessage("Could not locate an agent app. Open the app folder first.");
    return;
  }

  const slug = toSkillSlug(name);
  const skillDir = vscode.Uri.joinPath(appRoot, "skills", slug);
  const target = vscode.Uri.joinPath(skillDir, "SKILL.md");

  try {
    await vscode.workspace.fs.stat(target);
    const overwrite = await vscode.window.showWarningMessage(
      `skills/${slug}/SKILL.md already exists. Overwrite?`,
      { modal: true },
      "Overwrite"
    );
    if (overwrite !== "Overwrite") {
      return;
    }
  } catch {
    /* does not exist — good */
  }

  await vscode.workspace.fs.createDirectory(skillDir);
  const content = buildSkillMarkdown({ name, description });
  await vscode.workspace.fs.writeFile(target, Buffer.from(content, "utf-8"));
  const doc = await vscode.workspace.openTextDocument(target);
  await vscode.window.showTextDocument(doc);
  vscode.window.showInformationMessage(`Created skills/${slug}/SKILL.md`);
}
