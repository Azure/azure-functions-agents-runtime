/**
 * Pure JSON-Schema validation wrapper around ajv. The schema is generated from
 * the runtime's Pydantic models (see eng/scripts/generate_frontmatter_jsonschema.py),
 * so it uses draft-07 with a few Pydantic-isms (e.g. a stray `gt` keyword) that
 * we tolerate with a permissive ajv config.
 */
import Ajv, { type ErrorObject, type ValidateFunction } from "ajv";

export type SchemaError = ErrorObject;

export function createValidator(schema: object): ValidateFunction {
  const ajv = new Ajv({
    strict: false,
    allErrors: true,
    allowUnionTypes: true,
    validateFormats: false,
  });
  return ajv.compile(schema);
}

export interface ValidationResult {
  valid: boolean;
  errors: SchemaError[];
}

export function validateData(validate: ValidateFunction, data: unknown): ValidationResult {
  const valid = validate(data) as boolean;
  return { valid, errors: valid ? [] : (validate.errors ?? []) };
}

/**
 * Turn an ajv error into a concise, human-friendly message.
 */
export function formatSchemaError(err: SchemaError): string {
  const where = err.instancePath ? err.instancePath.replace(/^\//, "").replace(/\//g, ".") : "(root)";
  if (err.keyword === "additionalProperties") {
    const extra = (err.params as { additionalProperty?: string }).additionalProperty;
    return `Unknown property "${extra}" at ${where}. Remove it or check the spelling.`;
  }
  if (err.keyword === "required") {
    const missing = (err.params as { missingProperty?: string }).missingProperty;
    return `Missing required property "${missing}"${where === "(root)" ? "" : ` in ${where}`}.`;
  }
  if (err.keyword === "type") {
    return `Property ${where} ${err.message}.`;
  }
  if (err.keyword === "enum") {
    const allowed = (err.params as { allowedValues?: unknown[] }).allowedValues;
    return `Property ${where} must be one of: ${(allowed ?? []).join(", ")}.`;
  }
  return `${where}: ${err.message ?? "invalid value"}.`;
}
