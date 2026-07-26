// Command cubelet-storage-metrics calls Cubelet's node-local CubeCoW metrics
// RPC over TCP and writes a stable JSON snapshot.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	cubebox "github.com/tencentcloud/CubeSandbox/Cubelet/api/services/cubebox/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type output struct {
	CollectedAt      string            `json:"collected_at"`
	Address          string            `json:"address"`
	RequestID        string            `json:"request_id"`
	ResponseRequest  string            `json:"response_request_id"`
	NodeID           string            `json:"node_id"`
	TimestampUnixNano int64             `json:"timestamp_unix_nano"`
	RetCode          int32             `json:"ret_code"`
	RetMessage       string            `json:"ret_message"`
	Metrics          map[string]uint64 `json:"metrics"`
}

func main() {
	address := flag.String("address", "127.0.0.1:9999", "Cubelet gRPC address")
	outputPath := flag.String("output", "", "JSON output file; stdout when empty")
	timeout := flag.Duration("timeout", 5*time.Second, "RPC timeout")
	flag.Parse()

	conn, err := grpc.NewClient(
		*address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "connect: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	requestID := fmt.Sprintf("storage-metrics-%d", time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	rsp, err := cubebox.NewCubeboxMgrClient(conn).GetStorageMetrics(
		ctx,
		&cubebox.GetStorageMetricsRequest{RequestID: requestID},
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "GetStorageMetrics: %v\n", err)
		os.Exit(1)
	}

	result := output{
		CollectedAt:       time.Now().UTC().Format(time.RFC3339Nano),
		Address:           *address,
		RequestID:         requestID,
		ResponseRequest:   rsp.GetRequestID(),
		NodeID:            rsp.GetNodeId(),
		TimestampUnixNano: rsp.GetTimestampUnixNano(),
		Metrics:           rsp.GetMetrics(),
	}
	if rsp.GetRet() != nil {
		result.RetCode = int32(rsp.GetRet().GetRetCode())
		result.RetMessage = rsp.GetRet().GetRetMsg()
	}

	data, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
	data = append(data, '\n')
	if *outputPath == "" {
		_, _ = os.Stdout.Write(data)
		return
	}
	if err := os.WriteFile(*outputPath, data, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "write %s: %v\n", *outputPath, err)
		os.Exit(1)
	}
}
