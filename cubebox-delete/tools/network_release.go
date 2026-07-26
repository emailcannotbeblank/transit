// Command network-release invokes network-agent's idempotent ReleaseNetwork RPC.
// It is intended for cleanup of node-local benchmark allocations.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	networkagentv1 "github.com/tencentcloud/CubeSandbox/Cubelet/pkg/networkagentclient/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	address := flag.String(
		"address",
		"unix:///tmp/cube/network-agent-grpc.sock",
		"network-agent gRPC endpoint",
	)
	timeout := flag.Duration("timeout", 10*time.Second, "RPC timeout")
	flag.Parse()
	if flag.NArg() != 1 || flag.Arg(0) == "" {
		fmt.Fprintln(os.Stderr, "usage: network-release [flags] SANDBOX_ID")
		os.Exit(2)
	}
	sandboxID := flag.Arg(0)

	conn, err := grpc.NewClient(
		*address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "connect: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	rsp, err := networkagentv1.NewNetworkAgentClient(conn).ReleaseNetwork(
		ctx,
		&networkagentv1.ReleaseNetworkRequest{
			SandboxId:     sandboxID,
			NetworkHandle: sandboxID,
			IdempotencyKey: fmt.Sprintf(
				"benchmark-cleanup-%d",
				time.Now().UnixNano(),
			),
		},
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ReleaseNetwork: %v\n", err)
		os.Exit(1)
	}

	data, err := json.MarshalIndent(
		map[string]any{
			"sandbox_id":      sandboxID,
			"released":        rsp.GetReleased(),
			"persist_metadata": rsp.GetPersistMetadata(),
		},
		"",
		"  ",
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(data))
	if !rsp.GetReleased() {
		os.Exit(1)
	}
}
