// Command cubelet-destroy invokes Cubelet's synchronous Destroy RPC over TCP.
// It is intended for node-local test cleanup and prints the response as JSON.
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	cubebox "github.com/tencentcloud/CubeSandbox/Cubelet/api/services/cubebox/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type result struct {
	RequestID  string            `json:"request_id"`
	SandboxID  string            `json:"sandbox_id"`
	RetCode    int32             `json:"ret_code"`
	RetMessage string            `json:"ret_message"`
	ExtInfo    map[string]string `json:"ext_info"`
}

func main() {
	address := flag.String("address", "127.0.0.1:9999", "Cubelet gRPC address")
	timeout := flag.Duration("timeout", 30*time.Second, "RPC timeout")
	flag.Parse()
	if flag.NArg() != 1 || flag.Arg(0) == "" {
		fmt.Fprintln(os.Stderr, "usage: cubelet-destroy [flags] SANDBOX_ID")
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

	requestID := fmt.Sprintf("cleanup-%d", time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	rsp, err := cubebox.NewCubeboxMgrClient(conn).Destroy(
		ctx,
		&cubebox.DestroyCubeSandboxRequest{
			RequestID: requestID,
			SandboxID: sandboxID,
		},
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Destroy: %v\n", err)
		os.Exit(1)
	}

	output := result{
		RequestID: rsp.GetRequestID(),
		SandboxID: sandboxID,
		ExtInfo:   make(map[string]string, len(rsp.GetExtInfo())),
	}
	if rsp.GetRet() != nil {
		output.RetCode = int32(rsp.GetRet().GetRetCode())
		output.RetMessage = rsp.GetRet().GetRetMsg()
	}
	for key, value := range rsp.GetExtInfo() {
		if decoded, err := base64.StdEncoding.DecodeString(string(value)); err == nil {
			output.ExtInfo[key] = string(decoded)
		} else {
			output.ExtInfo[key] = string(value)
		}
	}

	data, err := json.MarshalIndent(output, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(data))
	if output.RetCode != 200 {
		os.Exit(1)
	}
}
