package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const parentPIDEnv = "AIWORKHUB_APP_SERVER_MUX_EXTENSION_HOST_PID"

func runWith(python string, target string, args []string) error {
	pythonArgs := append([]string{}, args...)
	if strings.EqualFold(filepath.Base(python), "py.exe") || strings.EqualFold(filepath.Base(python), "py") {
		pythonArgs = append([]string{"-3", target}, pythonArgs...)
	} else {
		pythonArgs = append([]string{target}, pythonArgs...)
	}
	cmd := exec.Command(python, pythonArgs...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), fmt.Sprintf("%s=%d", parentPIDEnv, os.Getppid()))
	return cmd.Run()
}

func main() {
	executable, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "AIWorkHub launcher cannot resolve its executable path")
		os.Exit(1)
	}
	targetBytes, err := os.ReadFile(executable + ".target")
	if err != nil {
		fmt.Fprintln(os.Stderr, "AIWorkHub launcher target is unavailable")
		os.Exit(1)
	}
	target := strings.TrimSpace(string(targetBytes))
	if target == "" {
		fmt.Fprintln(os.Stderr, "AIWorkHub launcher target is empty")
		os.Exit(1)
	}

	err = runWith("py.exe", target, os.Args[1:])
	if err != nil {
		if _, ok := err.(*exec.ExitError); ok {
			os.Exit(err.(*exec.ExitError).ExitCode())
		}
		err = runWith("python.exe", target, os.Args[1:])
	}
	if err == nil {
		return
	}
	if exitErr, ok := err.(*exec.ExitError); ok {
		os.Exit(exitErr.ExitCode())
	}
	fmt.Fprintln(os.Stderr, "AIWorkHub launcher could not start Python:", err)
	os.Exit(1)
}
