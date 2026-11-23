package main

import (
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/discovery"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/hub"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/server"
	"github.com/aiaimimi0920/g2a-intelligent-hub/internal/store"
)

func main() {
	originAddr := flag.String("origin-addr", "127.0.0.1:19210", "Origin Interface listen address")
	coreAddr := flag.String("core-addr", "127.0.0.1:19220", "Core Interface listen address")
	consentAddr := flag.String("consent-addr", "127.0.0.1:19230", "Trusted Player Dashboard & Consent listen address (loopback only)")
	ledgerPath := flag.String("ledger", "", "Optional durable JSON ledger file path")
	autoScope := flag.Bool("auto-approve-scope", false, "Automatically approve narrowed session scopes (for local pairing/testing)")
	originToken := flag.String("origin-token", "", "Shared Bearer token for Origin <-> Hub calls")
	coreTokensRaw := flag.String("core-tokens", "", "Comma-separated token=core_id mappings (empty allows bearer=<core_id> in local dev)")
	consentToken := flag.String("consent-token", "", "Bearer token protecting Trusted Player Consent port")
	tlsCert := flag.String("tls-cert", "", "Optional TLS 1.3 certificate file for Core Interface")
	tlsKey := flag.String("tls-key", "", "Optional TLS 1.3 private key file for Core Interface")
	rateLimit := flag.Int("rate-limit-per-sec", 240, "Max requests per second per session before 429 resource_limit")
	runtimeFile := flag.String("runtime-file", discovery.DefaultRuntimeFilePath(), "Local Hub discovery manifest path (set empty to disable)")
	noRuntimeFile := flag.Bool("no-runtime-file", false, "Disable writing local Hub discovery manifest")
	openDashboard := flag.Bool("open-dashboard", false, "Open Trusted Player Security Dashboard in default browser on startup")
	printPortsJSON := flag.Bool("print-ports-json", false, "Print bound addresses as single-line JSON on startup")
	flag.Parse()

	persistence, err := store.NewPersistencePort(*ledgerPath)
	if err != nil {
		log.Fatalf("init persistence: %v", err)
	}

	cfg := hub.DefaultConfig()
	cfg.AutoApproveScope = *autoScope
	cfg.OriginSharedToken = *originToken
	cfg.RateLimitPerWindow = *rateLimit
	engine := hub.NewEngine(cfg, persistence, nil, nil, nil)

	stopReaper := make(chan struct{})
	defer close(stopReaper)
	engine.StartBackgroundLeaseReaper(stopReaper, 2*time.Second)

	coreMap := make(map[string]string)
	if *coreTokensRaw != "" {
		for _, pair := range strings.Split(*coreTokensRaw, ",") {
			kv := strings.SplitN(strings.TrimSpace(pair), "=", 2)
			if len(kv) == 2 {
				coreMap[kv[0]] = kv[1]
			}
		}
	}

	originLn, err := net.Listen("tcp", *originAddr)
	if err != nil {
		log.Fatalf("listen origin %s: %v", *originAddr, err)
	}
	coreLn, err := net.Listen("tcp", *coreAddr)
	if err != nil {
		log.Fatalf("listen core %s: %v", *coreAddr, err)
	}
	consentLn, err := net.Listen("tcp", *consentAddr)
	if err != nil {
		log.Fatalf("listen consent %s: %v", *consentAddr, err)
	}

	auth := server.AuthRegistry{
		OriginToken:  *originToken,
		CoreTokens:   coreMap,
		ConsentToken: *consentToken,
		OriginAddr:   originLn.Addr().String(),
		CoreAddr:     coreLn.Addr().String(),
	}

	go func() { _ = http.Serve(originLn, server.NewOriginHandler(engine, auth)) }()
	go func() {
		coreSrv := &http.Server{
			Handler:   server.NewCoreHandler(engine, auth),
			TLSConfig: &tls.Config{MinVersion: tls.VersionTLS13},
		}
		if *tlsCert != "" && *tlsKey != "" {
			_ = coreSrv.ServeTLS(coreLn, *tlsCert, *tlsKey)
		} else {
			_ = coreSrv.Serve(coreLn)
		}
	}()
	go func() { _ = http.Serve(consentLn, server.NewTrustedConsentHandler(engine, auth)) }()

	manifestPath := *runtimeFile
	if *noRuntimeFile {
		manifestPath = ""
	}
	if manifestPath != "" {
		_ = discovery.WriteRuntimeManifest(
			manifestPath,
			originLn.Addr().String(),
			coreLn.Addr().String(),
			consentLn.Addr().String(),
			*tlsCert != "" && *tlsKey != "",
		)
		defer discovery.RemoveRuntimeManifestIfOwned(manifestPath)
	}

	info := map[string]string{
		"g2a_version":   "0.0.1",
		"origin_addr":   originLn.Addr().String(),
		"core_addr":     coreLn.Addr().String(),
		"consent_addr":  consentLn.Addr().String(),
		"dashboard_url": "http://" + consentLn.Addr().String() + "/",
		"runtime_file":  manifestPath,
	}
	if *openDashboard {
		openBrowser(info["dashboard_url"])
	}
	if *printPortsJSON {
		raw, _ := json.Marshal(info)
		fmt.Println(string(raw))
	} else {
		log.Printf("G2A Intelligent Hub V0.0.1 ready | Origin=%s | Core=%s | Dashboard=%s",
			info["origin_addr"], info["core_addr"], info["dashboard_url"])
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}
