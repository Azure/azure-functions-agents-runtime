package main

import (
	"bufio"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

const (
	protocolVersion  = "2025-11-25"
	maxMCPLine       = 1024 * 1024
	maxRequestFrame  = 1024 * 1024
	maxResponseFrame = 4 * 1024 * 1024
	startupTimeout   = 30 * time.Second
)

type parameter struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

type contract struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
	Parameters  []parameter    `json:"parameters"`
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type callParams struct {
	Name      string                     `json:"name"`
	Arguments map[string]json.RawMessage `json:"arguments"`
}

type invocationRequest struct {
	Generation string                     `json:"generation"`
	ID         string                     `json:"id"`
	Arguments  map[string]json.RawMessage `json:"arguments"`
}

type invocationResponse struct {
	Generation string `json:"generation"`
	ID         string `json:"id"`
	OK         bool   `json:"ok"`
	Kind       string `json:"kind"`
	Text       string `json:"text"`
	Error      string `json:"error"`
}

type handshake struct {
	Token      string `json:"token"`
	Generation string `json:"generation"`
}

type call struct {
	request rpcRequest
	params  callParams
	id      string
}

type startupState struct {
	listener   net.Listener
	command    *exec.Cmd
	boundary   *processBoundary
	generation string
	token      string
}

type customerProcess struct {
	mu         sync.Mutex
	executable string
	startup    *startupState
	command    *exec.Cmd
	boundary   *processBoundary
	connection net.Conn
	reader     *bufio.Reader
	generation string
	token      string
	activeID   string
	cancelled  map[string]bool
	known      map[string]bool
}

var (
	outputMu sync.Mutex
)

func randomHex(bytes int) (string, error) {
	value := make([]byte, bytes)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return hex.EncodeToString(value), nil
}

func decodeContract(encoded string) (contract, error) {
	padding := strings.Repeat("=", (4-len(encoded)%4)%4)
	payload, err := base64.URLEncoding.DecodeString(encoded + padding)
	if err != nil {
		return contract{}, err
	}
	var result contract
	err = json.Unmarshal(payload, &result)
	return result, err
}

func send(payload any) {
	outputMu.Lock()
	defer outputMu.Unlock()
	encoded, err := json.Marshal(payload)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return
	}
	os.Stdout.Write(encoded)
	os.Stdout.Write([]byte("\n"))
}

func rpcResult(id json.RawMessage, result any) {
	send(map[string]any{"jsonrpc": "2.0", "id": id, "result": result})
}

func rpcError(id json.RawMessage, code int, message string) {
	send(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"error":   map[string]any{"code": code, "message": message},
	})
}

func toolError(message string) map[string]any {
	return map[string]any{
		"content": []map[string]string{{"type": "text", "text": message}},
		"isError": true,
	}
}

func writeFrame(writer io.Writer, value any, maximum int) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if len(payload) > maximum {
		return fmt.Errorf("frame exceeds %d bytes", maximum)
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	if _, err = writer.Write(header[:]); err != nil {
		return err
	}
	_, err = writer.Write(payload)
	return err
}

func readFrame(reader *bufio.Reader, maximum int) ([]byte, error) {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return nil, err
	}
	size := int(binary.BigEndian.Uint32(header[:]))
	if size > maximum {
		return nil, fmt.Errorf("frame exceeds %d bytes", maximum)
	}
	payload := make([]byte, size)
	_, err := io.ReadFull(reader, payload)
	return payload, err
}

func (process *customerProcess) resetLocked() {
	if process.connection != nil {
		process.connection.Close()
	}
	if process.startup != nil {
		process.startup.listener.Close()
		process.startup.boundary.terminate(process.startup.command)
		process.startup.boundary.close()
		process.startup.command.Wait()
	}
	if process.boundary != nil && process.command != nil {
		process.boundary.terminate(process.command)
		process.boundary.close()
	}
	if process.command != nil {
		process.command.Wait()
	}
	process.command = nil
	process.boundary = nil
	process.startup = nil
	process.connection = nil
	process.reader = nil
	process.generation = ""
	process.token = ""
	process.activeID = ""
}

func (process *customerProcess) reset() {
	process.mu.Lock()
	defer process.mu.Unlock()
	process.resetLocked()
}

func (process *customerProcess) start(id string) error {
	process.mu.Lock()
	if process.connection != nil {
		process.mu.Unlock()
		return nil
	}
	process.mu.Unlock()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	tcpListener := listener.(*net.TCPListener)
	tcpListener.SetDeadline(time.Now().Add(startupTimeout))
	token, err := randomHex(32)
	if err != nil {
		listener.Close()
		return err
	}
	generation, err := randomHex(32)
	if err != nil {
		listener.Close()
		return err
	}
	command := exec.Command(process.executable)
	command.Stdout = os.Stderr
	command.Stderr = os.Stderr
	command.Env = append(
		os.Environ(),
		"AZURE_FUNCTIONS_AGENTS_GO_ADDRESS="+listener.Addr().String(),
		"AZURE_FUNCTIONS_AGENTS_GO_TOKEN="+token,
		"AZURE_FUNCTIONS_AGENTS_GO_GENERATION="+generation,
	)
	boundary, err := newProcessBoundary(command)
	if err != nil {
		listener.Close()
		return err
	}
	if err := command.Start(); err != nil {
		listener.Close()
		boundary.close()
		return err
	}
	if err := boundary.afterStart(command); err != nil {
		boundary.terminate(command)
		boundary.close()
		command.Wait()
		return err
	}
	state := &startupState{
		listener:   listener,
		command:    command,
		boundary:   boundary,
		generation: generation,
		token:      token,
	}
	process.mu.Lock()
	if process.activeID != id {
		process.mu.Unlock()
		listener.Close()
		boundary.terminate(command)
		boundary.close()
		command.Wait()
		return errors.New("Go tool call was cancelled")
	}
	process.startup = state
	process.mu.Unlock()
	connection, err := listener.Accept()
	if err != nil {
		process.mu.Lock()
		if process.startup == state {
			process.resetLocked()
		}
		process.mu.Unlock()
		return fmt.Errorf("customer process did not connect: %w", err)
	}
	connection.SetDeadline(time.Now().Add(startupTimeout))
	reader := bufio.NewReader(connection)
	payload, err := readFrame(reader, maxRequestFrame)
	if err != nil {
		connection.Close()
		process.mu.Lock()
		if process.startup == state {
			process.resetLocked()
		}
		process.mu.Unlock()
		return fmt.Errorf("customer handshake failed: %w", err)
	}
	var greeting handshake
	if err := json.Unmarshal(payload, &greeting); err != nil ||
		subtle.ConstantTimeCompare([]byte(greeting.Token), []byte(token)) != 1 ||
		greeting.Generation != generation {
		connection.Close()
		process.mu.Lock()
		if process.startup == state {
			process.resetLocked()
		}
		process.mu.Unlock()
		return errors.New("customer process authentication failed")
	}
	connection.SetDeadline(time.Time{})
	listener.Close()
	process.mu.Lock()
	defer process.mu.Unlock()
	if process.startup != state || process.activeID != id {
		connection.Close()
		return errors.New("Go tool call was cancelled")
	}
	process.startup = nil
	process.command = command
	process.boundary = boundary
	process.connection = connection
	process.reader = reader
	process.generation = generation
	process.token = token
	return nil
}

func (process *customerProcess) invoke(id string, arguments map[string]json.RawMessage) (invocationResponse, error) {
	if err := process.start(id); err != nil {
		return invocationResponse{}, err
	}
	process.mu.Lock()
	if process.activeID != id || process.connection == nil || process.reader == nil {
		process.mu.Unlock()
		return invocationResponse{}, errors.New("Go tool call was cancelled")
	}
	connection := process.connection
	reader := process.reader
	generation := process.generation
	process.mu.Unlock()

	request := invocationRequest{
		Generation: generation,
		ID:         id,
		Arguments:  arguments,
	}
	if err := writeFrame(connection, request, maxRequestFrame); err != nil {
		process.resetGeneration(generation)
		return invocationResponse{}, err
	}
	payload, err := readFrame(reader, maxResponseFrame)
	if err != nil {
		process.resetGeneration(generation)
		return invocationResponse{}, err
	}
	var response invocationResponse
	if err := json.Unmarshal(payload, &response); err != nil {
		process.resetGeneration(generation)
		return invocationResponse{}, err
	}
	if response.Generation != generation || response.ID != id {
		process.resetGeneration(generation)
		return invocationResponse{}, errors.New("customer response identity mismatch")
	}
	process.mu.Lock()
	if process.generation == generation && process.activeID == id {
		process.activeID = ""
	}
	process.mu.Unlock()
	return response, nil
}

func (process *customerProcess) resetGeneration(generation string) {
	process.mu.Lock()
	defer process.mu.Unlock()
	if process.generation == generation {
		process.resetLocked()
	}
}

func (process *customerProcess) begin(id string) bool {
	process.mu.Lock()
	defer process.mu.Unlock()
	if process.cancelled[id] {
		delete(process.cancelled, id)
		return false
	}
	process.activeID = id
	return true
}

func (process *customerProcess) register(id string) bool {
	process.mu.Lock()
	defer process.mu.Unlock()
	if process.known[id] {
		return false
	}
	process.known[id] = true
	return true
}

func (process *customerProcess) finish(id string) {
	process.mu.Lock()
	defer process.mu.Unlock()
	delete(process.known, id)
	delete(process.cancelled, id)
	if process.activeID == id {
		process.activeID = ""
	}
}

func (process *customerProcess) takeCancellation(id string) bool {
	process.mu.Lock()
	defer process.mu.Unlock()
	if !process.cancelled[id] {
		return false
	}
	delete(process.cancelled, id)
	return true
}

func (process *customerProcess) cancel(id string) {
	process.mu.Lock()
	defer process.mu.Unlock()
	if !process.known[id] {
		return
	}
	process.cancelled[id] = true
	if process.activeID == id {
		process.resetLocked()
	}
}

func validateArguments(tool contract, arguments map[string]json.RawMessage) error {
	if arguments == nil {
		arguments = map[string]json.RawMessage{}
	}
	allowed := make(map[string]bool, len(tool.Parameters))
	for _, parameter := range tool.Parameters {
		allowed[parameter.Name] = true
		if _, ok := arguments[parameter.Name]; !ok {
			return fmt.Errorf("missing required argument: %s", parameter.Name)
		}
	}
	for name := range arguments {
		if !allowed[name] {
			return fmt.Errorf("unexpected argument: %s", name)
		}
	}
	return nil
}

func handleCall(
	tool contract,
	customer *customerProcess,
	item call,
) {
	if item.params.Name != tool.Name {
		rpcResult(item.request.ID, toolError("Unknown tool: "+item.params.Name))
		customer.finish(item.id)
		return
	}
	if err := validateArguments(tool, item.params.Arguments); err != nil {
		rpcResult(item.request.ID, toolError(err.Error()))
		customer.finish(item.id)
		return
	}
	if !customer.begin(item.id) {
		rpcResult(item.request.ID, toolError("Go tool call was cancelled."))
		customer.finish(item.id)
		return
	}
	response, err := customer.invoke(item.id, item.params.Arguments)
	if err != nil {
		if customer.takeCancellation(item.id) {
			rpcResult(item.request.ID, toolError("Go tool call was cancelled."))
			customer.finish(item.id)
			return
		}
		fmt.Fprintln(os.Stderr, err)
		rpcResult(item.request.ID, toolError(err.Error()))
		customer.finish(item.id)
		return
	}
	if customer.takeCancellation(item.id) {
		rpcResult(item.request.ID, toolError("Go tool call was cancelled."))
		customer.finish(item.id)
		return
	}
	if !response.OK {
		rpcResult(item.request.ID, toolError(response.Error))
		customer.finish(item.id)
		return
	}
	content := []map[string]string{}
	if response.Kind == "text" {
		content = append(content, map[string]string{"type": "text", "text": response.Text})
	}
	rpcResult(item.request.ID, map[string]any{"content": content, "isError": false})
	customer.finish(item.id)
}

func requestID(id json.RawMessage) string {
	return string(id)
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "Go MCP adapter requires contract and customer executable.")
		os.Exit(2)
	}
	tool, err := decodeContract(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	customer := &customerProcess{
		executable: os.Args[2],
		cancelled:  map[string]bool{},
		known:      map[string]bool{},
	}
	defer customer.reset()
	calls := make(chan call, 64)
	go func() {
		for item := range calls {
			handleCall(tool, customer, item)
		}
	}()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), maxMCPLine)
	for scanner.Scan() {
		var request rpcRequest
		if err := json.Unmarshal(scanner.Bytes(), &request); err != nil {
			rpcError(nil, -32700, "Invalid JSON.")
			continue
		}
		switch request.Method {
		case "initialize":
			var params struct {
				ProtocolVersion string `json:"protocolVersion"`
			}
			json.Unmarshal(request.Params, &params)
			if params.ProtocolVersion != protocolVersion {
				rpcError(request.ID, -32602, "Unsupported protocol version.")
				continue
			}
			rpcResult(request.ID, map[string]any{
				"protocolVersion": protocolVersion,
				"capabilities":    map[string]any{"tools": map[string]bool{"listChanged": false}},
				"serverInfo":      map[string]string{"name": "azure-functions-agents-go-tool", "version": "1.0.0"},
			})
		case "ping":
			rpcResult(request.ID, map[string]any{})
		case "tools/list":
			rpcResult(request.ID, map[string]any{"tools": []map[string]any{{
				"name": tool.Name, "description": tool.Description, "inputSchema": tool.InputSchema,
			}}})
		case "tools/call":
			var params callParams
			if err := json.Unmarshal(request.Params, &params); err != nil {
				rpcResult(request.ID, toolError("Invalid tool arguments."))
				continue
			}
			item := call{request: request, params: params, id: requestID(request.ID)}
			if !customer.register(item.id) {
				rpcResult(request.ID, toolError("Duplicate Go tool call ID."))
				continue
			}
			select {
			case calls <- item:
			default:
				rpcResult(request.ID, toolError("Go tool is busy."))
				customer.finish(item.id)
			}
		case "notifications/cancelled":
			var params struct {
				RequestID json.RawMessage `json:"requestId"`
			}
			if json.Unmarshal(request.Params, &params) == nil {
				id := requestID(params.RequestID)
				customer.cancel(id)
			}
		default:
			if strings.HasPrefix(request.Method, "notifications/") {
				continue
			}
			rpcError(request.ID, -32601, "Method not found: "+request.Method)
		}
	}
	close(calls)
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}
