package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"strings"
)

type inspection struct {
	ReturnKind string   `json:"returnKind"`
	Imports    []string `json:"imports"`
}

func portableType(name string) types.Type {
	switch name {
	case "string":
		return types.Typ[types.String]
	case "integer":
		return types.Typ[types.Int64]
	case "number":
		return types.Typ[types.Float64]
	case "boolean":
		return types.Typ[types.Bool]
	default:
		return nil
	}
}

func classifyResults(results *types.Tuple) (string, error) {
	errorType := types.Universe.Lookup("error").Type()
	switch results.Len() {
	case 0:
		return "none", nil
	case 1:
		if types.Identical(results.At(0).Type(), errorType) {
			return "error", nil
		}
		return "value", nil
	case 2:
		if !types.Identical(results.At(1).Type(), errorType) {
			return "", fmt.Errorf("second return value must be error")
		}
		return "value_error", nil
	default:
		return "", fmt.Errorf("function must return no value, T, error, or (T, error)")
	}
}

func run(sourcePath string, entrypoint string, parameterList string) (inspection, error) {
	fileSet := token.NewFileSet()
	file, err := parser.ParseFile(fileSet, sourcePath, nil, parser.AllErrors)
	if err != nil {
		return inspection{}, err
	}
	if file.Name.Name != "main" {
		return inspection{}, fmt.Errorf("Go source must declare package main")
	}
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if ok && function.Recv == nil && function.Name.Name == "main" {
			return inspection{}, fmt.Errorf("customer Go source must not declare main")
		}
	}
	configuration := types.Config{Importer: importer.Default()}
	packageValue, err := configuration.Check("azurefunctions.localtool/customer", fileSet, []*ast.File{file}, nil)
	if err != nil {
		return inspection{}, err
	}
	object := packageValue.Scope().Lookup(entrypoint)
	function, ok := object.(*types.Func)
	if !ok || !object.Exported() {
		return inspection{}, fmt.Errorf("entrypoint %q is not an exported package-level function", entrypoint)
	}
	signature, ok := function.Type().(*types.Signature)
	if !ok || signature.Recv() != nil {
		return inspection{}, fmt.Errorf("entrypoint %q is not a package-level function", entrypoint)
	}
	if signature.TypeParams() != nil && signature.TypeParams().Len() != 0 {
		return inspection{}, fmt.Errorf("entrypoint must not be generic")
	}
	if signature.Variadic() {
		return inspection{}, fmt.Errorf("entrypoint must not be variadic")
	}
	parameters := []string{}
	if parameterList != "" {
		parameters = strings.Split(parameterList, ",")
	}
	if signature.Params().Len() != len(parameters) {
		return inspection{}, fmt.Errorf(
			"entrypoint expects %d parameters; tool.md declares %d",
			signature.Params().Len(),
			len(parameters),
		)
	}
	for index, parameter := range parameters {
		expected := portableType(parameter)
		if expected == nil {
			return inspection{}, fmt.Errorf("unsupported portable parameter type %q", parameter)
		}
		if !types.Identical(signature.Params().At(index).Type(), expected) {
			return inspection{}, fmt.Errorf(
				"parameter %d must be %s",
				index+1,
				expected.String(),
			)
		}
	}
	returnKind, err := classifyResults(signature.Results())
	if err != nil {
		return inspection{}, err
	}
	imports := make([]string, 0, len(file.Imports))
	for _, imported := range file.Imports {
		imports = append(imports, strings.Trim(imported.Path.Value, `"`))
	}
	return inspection{ReturnKind: returnKind, Imports: imports}, nil
}

func main() {
	arguments := os.Args[1:]
	if len(arguments) > 0 && arguments[0] == "--" {
		arguments = arguments[1:]
	}
	if len(arguments) != 3 {
		fmt.Fprintln(os.Stderr, "usage: inspector <source> <entrypoint> <parameter-types>")
		os.Exit(2)
	}
	result, err := run(arguments[0], arguments[1], arguments[2])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
