package g2acore

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"
)

// ValidateStrictJSONBytes verifies UTF-8 I-JSON compliance: no duplicate object keys,
// no lone UTF-16 surrogates, no trailing tokens, and max nesting depth 64.
func ValidateStrictJSONBytes(raw []byte) error {
	if !utf8.Valid(raw) {
		return errors.New("invalid UTF-8 in JSON payload")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := checkStrictValue(dec, 0); err != nil {
		return err
	}
	if tok, err := dec.Token(); err != io.EOF {
		return fmt.Errorf("trailing token after JSON value: %v (%v)", tok, err)
	}
	if bytes.Contains(raw, []byte(`\uD`)) || bytes.Contains(raw, []byte(`\ud`)) {
		if err := rejectLoneSurrogates(raw); err != nil {
			return err
		}
	}
	return nil
}

func rejectLoneSurrogates(b []byte) error {
	for i := 0; i+6 <= len(b); i++ {
		if b[i] != '\\' || b[i+1] != 'u' {
			continue
		}
		// Count preceding backslashes to avoid false positive on \\uXXXX
		bs := 0
		for j := i - 1; j >= 0 && b[j] == '\\'; j-- {
			bs++
		}
		if bs%2 == 1 {
			continue
		}
		u1, ok := parseHex4(b[i+2 : i+6])
		if !ok {
			return errors.New("invalid \\uXXXX hex escape")
		}
		if u1 >= 0xD800 && u1 <= 0xDBFF {
			if i+12 > len(b) || b[i+6] != '\\' || b[i+7] != 'u' {
				return errors.New("lone high UTF-16 surrogate in JSON string")
			}
			u2, ok2 := parseHex4(b[i+8 : i+12])
			if !ok2 || u2 < 0xDC00 || u2 > 0xDFFF {
				return errors.New("unpaired high UTF-16 surrogate in JSON string")
			}
			i += 11
		} else if u1 >= 0xDC00 && u1 <= 0xDFFF {
			return errors.New("lone low UTF-16 surrogate in JSON string")
		}
	}
	return nil
}

func parseHex4(s []byte) (uint16, bool) {
	var v uint16
	for _, c := range s {
		v <<= 4
		switch {
		case c >= '0' && c <= '9':
			v |= uint16(c - '0')
		case c >= 'a' && c <= 'f':
			v |= uint16(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			v |= uint16(c - 'A' + 10)
		default:
			return 0, false
		}
	}
	return v, true
}

func checkStrictValue(dec *json.Decoder, depth int) error {
	if depth > 64 {
		return errors.New("JSON nesting depth exceeds 64")
	}
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	d, ok := tok.(json.Delim)
	if !ok {
		return nil
	}
	switch d {
	case '{':
		seen := make(map[string]struct{})
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return err
			}
			ks, ok := kt.(string)
			if !ok {
				return errors.New("expected string key in JSON object")
			}
			if _, dup := seen[ks]; dup {
				return fmt.Errorf("duplicate JSON object key %q", ks)
			}
			seen[ks] = struct{}{}
			if err := checkStrictValue(dec, depth+1); err != nil {
				return err
			}
		}
		_, err := dec.Token()
		return err
	case '[':
		for dec.More() {
			if err := checkStrictValue(dec, depth+1); err != nil {
				return err
			}
		}
		_, err := dec.Token()
		return err
	default:
		return fmt.Errorf("unexpected delimiter %v", d)
	}
}

func UnmarshalStrict(raw []byte, dst any) error {
	if err := ValidateStrictJSONBytes(raw); err != nil {
		return err
	}
	return json.Unmarshal(raw, dst)
}

func CanonicalizeUTF8(val any) ([]byte, error) {
	raw, err := json.Marshal(val)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var generic any
	if err := dec.Decode(&generic); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	if err := writeJCS(&buf, generic); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func DigestSHA256(domainTag string, val any) (string, error) {
	canon, err := CanonicalizeUTF8(val)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	h.Write([]byte(domainTag))
	h.Write([]byte{0})
	h.Write(canon)
	return "sha256:" + hex.EncodeToString(h.Sum(nil)), nil
}

func compareUTF16(a, b string) bool {
	ua := utf16.Encode([]rune(a))
	ub := utf16.Encode([]rune(b))
	n := len(ua)
	if len(ub) < n {
		n = len(ub)
	}
	for i := 0; i < n; i++ {
		if ua[i] != ub[i] {
			return ua[i] < ub[i]
		}
	}
	return len(ua) < len(ub)
}

func writeJCS(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if x {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		f, err := x.Float64()
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return errors.New("non-finite number forbidden in JCS")
		}
		writeJCSFloat(buf, f)
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return errors.New("non-finite number forbidden in JCS")
		}
		writeJCSFloat(buf, x)
	case string:
		writeJCSString(buf, x)
	case []any:
		buf.WriteByte('[')
		for i, elem := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeJCS(buf, elem); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool { return compareUTF16(keys[i], keys[j]) })
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeJCSString(buf, k)
			buf.WriteByte(':')
			if err := writeJCS(buf, x[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("unsupported JCS value %T", v)
	}
	return nil
}

func writeJCSFloat(buf *bytes.Buffer, f float64) {
	if f == 0 {
		buf.WriteByte('0')
		return
	}
	abs := math.Abs(f)
	if abs >= 1e-6 && abs < 1e21 {
		buf.WriteString(strconv.FormatFloat(f, 'f', -1, 64))
		return
	}
	s := strconv.FormatFloat(f, 'e', -1, 64)
	// strconv uses e+07 / e-07; ECMA-262 strips leading zero in exponent (e+7 / e-7)
	if idx := bytes.IndexByte([]byte(s), 'e'); idx >= 0 && idx+2 < len(s) {
		sign := s[idx+1]
		expNum, err := strconv.Atoi(s[idx+2:])
		if err == nil {
			buf.WriteString(s[:idx+1])
			buf.WriteByte(sign)
			buf.WriteString(strconv.Itoa(expNum))
			return
		}
	}
	buf.WriteString(s)
}

func writeJCSString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\t':
			buf.WriteString(`\t`)
		case '\n':
			buf.WriteString(`\n`)
		case '\f':
			buf.WriteString(`\f`)
		case '\r':
			buf.WriteString(`\r`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}
