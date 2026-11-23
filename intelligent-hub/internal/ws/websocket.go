package ws

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
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
		return nil, errors.New("missing Upgrade: websocket header")
	}
	key := strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))
	if key == "" {
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

func (c *Conn) writeFrame(opcode byte, payload []byte) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()

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
func (c *Conn) ReadLoop() {
	for {
		b0, err := c.bufrw.ReadByte()
		if err != nil {
			return
		}
		b1, err := c.bufrw.ReadByte()
		if err != nil {
			return
		}
		opcode := b0 & 0x0F
		masked := (b1 & 0x80) != 0
		length := int64(b1 & 0x7F)
		if length == 126 {
			var ext [2]byte
			if _, err := c.bufrw.Read(ext[:]); err != nil {
				return
			}
			length = int64(binary.BigEndian.Uint16(ext[:]))
		} else if length == 127 {
			var ext [8]byte
			if _, err := c.bufrw.Read(ext[:]); err != nil {
				return
			}
			length = int64(binary.BigEndian.Uint64(ext[:]))
		}
		var maskKey [4]byte
		if masked {
			if _, err := c.bufrw.Read(maskKey[:]); err != nil {
				return
			}
		}
		if length > (1 << 20) {
			return
		}
		data := make([]byte, length)
		readOff := int64(0)
		for readOff < length {
			nr, rErr := c.bufrw.Read(data[readOff:])
			if rErr != nil {
				return
			}
			readOff += int64(nr)
		}
		if masked {
			for i := range data {
				data[i] ^= maskKey[i%4]
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
