package discovery

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

type RuntimeManifest struct {
	G2AVersion   string `json:"g2a_version"`
	PID          int    `json:"pid"`
	StartedAtMs  int64  `json:"started_at_ms"`
	OriginURL    string `json:"origin_url"`
	CoreURL      string `json:"core_url"`
	DashboardURL string `json:"dashboard_url"`
}

// DefaultRuntimeFilePath returns the per-user OS discovery file path shared across local games and Cores.
func DefaultRuntimeFilePath() string {
	if envOverride := os.Getenv("G2A_HUB_RUNTIME_FILE"); envOverride != "" {
		return envOverride
	}
	if runtime.GOOS == "windows" {
		if localAppData := os.Getenv("LOCALAPPDATA"); localAppData != "" {
			return filepath.Join(localAppData, "G2A", "hub.runtime.json")
		}
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return filepath.Join(os.TempDir(), "g2a-hub.runtime.json")
	}
	return filepath.Join(home, ".g2a", "hub.runtime.json")
}

func WriteRuntimeManifest(path string, originAddr, coreAddr, consentAddr string, tlsEnabled bool) error {
	if path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	coreScheme := "http"
	if tlsEnabled {
		coreScheme = "https"
	}
	m := RuntimeManifest{
		G2AVersion:   "0.0.1",
		PID:          os.Getpid(),
		StartedAtMs:  time.Now().UnixMilli(),
		OriginURL:    "http://" + originAddr,
		CoreURL:      coreScheme + "://" + coreAddr,
		DashboardURL: "http://" + consentAddr + "/",
	}
	raw, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if err := os.Rename(tmp, path); err == nil {
			return nil
		} else {
			lastErr = err
			time.Sleep(2 * time.Millisecond)
		}
	}
	return lastErr
}

func RemoveRuntimeManifestIfOwned(path string) {
	if path == "" {
		return
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var m RuntimeManifest
	if err := json.Unmarshal(raw, &m); err == nil && m.PID == os.Getpid() {
		_ = os.Remove(path)
	}
}
