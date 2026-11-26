package ws

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// Conn is a minimal RFC 6455 server WebSocket connection supporting text frames, ping/pong, and clean close.
type Conn struct {
	netConn net.Conn
	bufrw   *bufio.ReadWriter
	writeMu sync.Mutex
}

func Upgrade(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "missing Upgrade: websocket header", http.StatusBadRequest)
		return nil, errors.New("missing Upgrade: websocket header")
	}
	if strings.TrimSpace(r.Header.Get("Sec-WebSocket-Version")) != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "Sec-WebSocket-Version: 13 required", http.StatusUpgradeRequired)
		return nil, errors.New("unsupported Sec-WebSocket-Version")
	}
	key := strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))
	if key == "" {
		http.Error(w, "missing Sec-WebSocket-Key", http.StatusBadRequest)
		return nil, errors.New("missing Sec-WebSocket-Key")
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		return nil, errors.New("http.Hijacker not supported")
	}
	netConn, bufrw, err := hj.Hijack()
	if err != nil {
		return nil, err
	}
	_ = netConn.SetDeadline(time.Time{})

	sum := sha1.Sum([]byte(key + wsGUID))
	accept := base64.StdEncoding.EncodeToString(sum[:])

	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + accept + "\r\n\r\n"
	if _, err := bufrw.WriteString(resp); err != nil {
		_ = netConn.Close()
		return nil, err
	}
	if err := bufrw.Flush(); err != nil {
		_ = netConn.Close()
		return nil, err
	}
	return &Conn{netConn: netConn, bufrw: bufrw}, nil
}

func (c *Conn) WriteText(payload []byte) error {
	return c.writeFrame(0x1, payload)
}

func (c *Conn) WritePing(payload []byte) error {
	return c.writeFrame(0x9, payload)
}

func (c *Conn) WriteClose(statusCode uint16) error {
	var codeBuf [2]byte
	binary.BigEndian.PutUint16(codeBuf[:], statusCode)
	return c.writeFrame(0x8, codeBuf[:])
}

func (c *Conn) writeFrame(opcode byte, payload []byte) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()

	_ = c.netConn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	var hdr [10]byte
	hdr[0] = 0x80 | (opcode & 0x0F) // FIN=1
	hdrLen := 2
	n := len(payload)
	switch {
	case n < 126:
		hdr[1] = byte(n)
	case n <= 65535:
		hdr[1] = 126
		binary.BigEndian.PutUint16(hdr[2:4], uint16(n))
		hdrLen = 4
	default:
		hdr[1] = 127
		binary.BigEndian.PutUint64(hdr[2:10], uint64(n))
		hdrLen = 10
	}
	if _, err := c.bufrw.Write(hdr[:hdrLen]); err != nil {
		return err
	}
	if n > 0 {
		if _, err := c.bufrw.Write(payload); err != nil {
			return err
		}
	}
	return c.bufrw.Flush()
}

// ReadLoop drains client control frames (ping -> pong, close -> return) until the peer disconnects.
// Enforces RFC 6455 §5.1 client masking, §5.2 RSV=0 invariants, §5.5 control-frame fragmentation/125B limits,
// TCP-fragment-safe io.ReadFull headers, 64-bit overflow bounds, and zero-alloc stack buffers for control frames.
func (c *Conn) ReadLoop() {
	const maxClientFrameBytes = 1 << 20
	var stackBuf [128]byte
	for {
		b0, err := c.bufrw.ReadByte()
		if err != nil {
			return
		}
		b1, err := c.bufrw.ReadByte()
		if err != nil {
			return
		}
		fin := (b0 & 0x80) != 0
		rsv := b0 & 0x70
		opcode := b0 & 0x0F
		masked := (b1 & 0x80) != 0
		if rsv != 0 {
			// RFC 6455 §5.2: RSV1..3 MUST be 0 when no extensions were negotiated.
			_ = c.WriteClose(1002)
			return
		}
		switch opcode {
		case 0x0, 0x1, 0x2, 0x8, 0x9, 0xA:
		default:
			_ = c.WriteClose(1002)
			return
		}
		if !masked {
			// RFC 6455 §5.1: server MUST close connection upon receiving an unmasked client frame.
			_ = c.WriteClose(1002)
			return
		}
		uLen := uint64(b1 & 0x7F)
		if opcode >= 0x8 && (!fin || uLen > 125) {
			// RFC 6455 §5.5: control frames MUST NOT be fragmented and MUST NOT exceed 125 payload bytes.
			_ = c.WriteClose(1002)
			return
		}
		if uLen == 126 {
			var ext [2]byte
			if _, err := io.ReadFull(c.bufrw, ext[:]); err != nil {
				return
			}
			uLen = uint64(binary.BigEndian.Uint16(ext[:]))
		} else if uLen == 127 {
			var ext [8]byte
			if _, err := io.ReadFull(c.bufrw, ext[:]); err != nil {
				return
			}
			uLen = binary.BigEndian.Uint64(ext[:])
		}
		if uLen > maxClientFrameBytes {
			_ = c.WriteClose(1009)
			return
		}
		var maskKey [4]byte
		if _, err := io.ReadFull(c.bufrw, maskKey[:]); err != nil {
			return
		}
		var data []byte
		if uLen <= uint64(len(stackBuf)) {
			data = stackBuf[:int(uLen)]
		} else {
			data = make([]byte, int(uLen))
		}
		if uLen > 0 {
			if _, err := io.ReadFull(c.bufrw, data); err != nil {
				return
			}
			for i := range data {
				data[i] ^= maskKey[i&3]
			}
		}
		if opcode == 0x8 { // Close
			_ = c.writeFrame(0x8, nil)
			return
		}
		if opcode == 0x9 { // Ping -> Pong
			_ = c.writeFrame(0xA, data)
		}
	}
}

func (c *Conn) Close() error {
	return c.netConn.Close()
}
