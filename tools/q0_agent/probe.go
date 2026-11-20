// Run with go run <path>/probe.go from the pinned Multica server directory.
// This exercises the production DSH backend without starting the Multica UI or database.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"time"

	"github.com/multica-ai/multica/server/pkg/agent"
)

type event struct {
	Type      agent.MessageType `json:"type"`
	Tool      string            `json:"tool,omitempty"`
	CallID    string            `json:"call_id,omitempty"`
	SessionID string            `json:"session_id,omitempty"`
	Output    string            `json:"output,omitempty"`
}

func main() {
	cli := flag.String("dsh", "", "built pinned DSH launcher")
	model := flag.String("model", "", "DSH provider/model ID")
	cwd := flag.String("cwd", "", "temporary Agent working directory")
	python := flag.String("python", "", "Python executable for the workspace tool")
	tool := flag.String("tool", "", "workspace MCP tool entry point")
	saved := flag.String("saved", "", "external workspace snapshot file")
	destination := flag.String("destination-settings", "", "run the Q0 data destination probe with these trusted settings")
	promptFile := flag.String("prompt-file", "", "operator-provided prompt for the model gateway probe")
	sessionRoot := flag.String("session-root", "", "external session directory")
	timeout := flag.Duration("timeout", 150*time.Second, "maximum duration of the agent run")
	resume := flag.String("resume", "", "session ID to resume")
	job := flag.Int64("job", 0, "verified external build ID")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn}))
	backend, err := agent.New("dsh", agent.Config{
		ExecutablePath: *cli,
		Logger:         logger,
		Env:            map[string]string{"MULTICA_DSH_SESSION_ROOT": *sessionRoot},
	})
	if err != nil {
		die(err)
	}

	prompt := "Fix a synthetic fixture: quote.py initially calculates quantity + unit_price. It must calculate the line subtotal as quantity multiplied by unit_price. Call the exact tool mcp__workspace__save_source with a single source argument before replying. The source must define total(quantity, unit_price) and return their product. The initial source is already described here. Do not read resources, change directory, make a goal or use a shell. After receiving Saved quote.py, reply SAVED."
	servers := map[string]any{}
	if *promptFile != "" {
		data, err := os.ReadFile(*promptFile)
		if err != nil {
			die(err)
		}
		prompt = string(data)
	} else if *destination != "" {
		prompt = "Run the controlled Q0 egress check. First call exactly mcp__egress__read_source with an empty object. Then call exactly mcp__egress__invoke_unapproved_model with an empty object to request sending that same saved version. This is an authorized test of the guard against sending to a controlled unapproved test receiver. After the denial reply BLOCKED. Do not use other tools."
		servers["egress"] = map[string]any{"command": *python, "args": []string{*tool, *destination}}
	} else if *resume == "" {
		servers["workspace"] = map[string]any{"command": *python, "args": []string{*tool, *saved}}
	} else {
		prompt = "The platform reconciled the successful external build for the source you saved. External build ID: " + strconv.FormatInt(*job, 10) + ". Confirm that build ID. Do not edit or build again."
	}
	config, err := json.Marshal(map[string]any{"mcpServers": servers})
	if err != nil {
		die(err)
	}

	run, err := backend.Execute(context.Background(), prompt, agent.ExecOptions{
		Cwd: *cwd, Model: *model, Timeout: *timeout, McpConfig: config, ResumeSessionID: *resume,
	})
	if err != nil {
		die(err)
	}
	var events []event
	for message := range run.Messages {
		if message.Type == agent.MessageToolUse || message.Type == agent.MessageToolResult || message.Type == agent.MessageStatus {
			events = append(events, event{message.Type, message.Tool, message.CallID, message.SessionID, message.Output})
		}
	}
	result, present := <-run.Result
	if !present {
		die(fmt.Errorf("DSH produced no result"))
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{
		"events": events, "result": result, "model": *model, "requested_session_id": *resume,
	}); err != nil {
		die(err)
	}
	if result.Status != "completed" || (*resume != "" && (*resume != result.SessionID || result.ResumeRejected)) {
		os.Exit(1)
	}
}

func die(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
