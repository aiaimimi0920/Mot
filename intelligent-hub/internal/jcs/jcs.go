package jcs

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
	"unsafe"
)

var (
	ErrDuplicateKey   = errors.New("duplicate JSON object key")
	ErrNonFiniteFloat = errors.New("non-finite float (NaN or Infinity) is forbidden")
	ErrInvalidJSON    = errors.New("invalid strict RFC 8259 JSON")
)

// ValidateStrictJSONBytes validates that raw UTF-8 bytes form a single strict RFC 8259 / I-JSON document
// with zero duplicate object keys (including escaped equivalents), zero unescaped control chars or lone surrogates,
// zero trailing tokens, and finite numbers — without allocating an AST on the hot request path.
func ValidateStrictJSONBytes(raw []byte) error {
	s := strictScanner{b: raw}
	s.skipWS()
	if err := s.scanValue(0); err != nil {
		return err
	}
	s.skipWS()
	if s.pos != len(s.b) {
		return ErrInvalidJSON
	}
	return nil
}

type strictScanner struct {
	b   []byte
	pos int
}

func (s *strictScanner) skipWS() {
	for s.pos < len(s.b) {
		switch s.b[s.pos] {
		case ' ', '\t', '\r', '\n':
			s.pos++
		default:
			return
		}
	}
}

func (s *strictScanner) scanValue(depth int) error {
	if depth > 64 || s.pos >= len(s.b) {
		return ErrInvalidJSON
	}
	switch s.b[s.pos] {
	case '{':
		return s.scanObject(depth + 1)
	case '[':
		return s.scanArray(depth + 1)
	case '"':
		_, err := s.scanString(false)
		return err
	case 't':
		return s.expectLiteral("true")
	case 'f':
		return s.expectLiteral("false")
	case 'n':
		return s.expectLiteral("null")
	default:
		if s.b[s.pos] == '-' || (s.b[s.pos] >= '0' && s.b[s.pos] <= '9') {
			return s.scanNumber()
		}
		return ErrInvalidJSON
	}
}

func (s *strictScanner) expectLiteral(lit string) error {
	if s.pos+len(lit) > len(s.b) {
		return ErrInvalidJSON
	}
	for i := 0; i < len(lit); i++ {
		if s.b[s.pos+i] != lit[i] {
			return ErrInvalidJSON
		}
	}
	s.pos += len(lit)
	return nil
}

func (s *strictScanner) scanObject(depth int) error {
	s.pos++ // consume '{'
	s.skipWS()
	if s.pos >= len(s.b) {
		return ErrInvalidJSON
	}
	if s.b[s.pos] == '}' {
		s.pos++
		return nil
	}

	// Stack-allocated fast path for <= 12 keys avoids heap map allocation on typical G2A envelopes!
	var smallKeys [12]string
	smallCount := 0
	var largeSet map[string]struct{}

	for {
		s.skipWS()
		if s.pos >= len(s.b) || s.b[s.pos] != '"' {
			return ErrInvalidJSON
		}
		key, err := s.scanString(true)
		if err != nil {
			return err
		}
		if largeSet != nil {
			if _, dup := largeSet[key]; dup {
				return fmt.Errorf("%w: %q", ErrDuplicateKey, key)
			}
			largeSet[key] = struct{}{}
		} else {
			for i := 0; i < smallCount; i++ {
				if smallKeys[i] == key {
					return fmt.Errorf("%w: %q", ErrDuplicateKey, key)
				}
			}
			if smallCount < len(smallKeys) {
				smallKeys[smallCount] = key
				smallCount++
			} else {
				largeSet = make(map[string]struct{}, 24)
				for i := 0; i < smallCount; i++ {
					largeSet[smallKeys[i]] = struct{}{}
				}
				largeSet[key] = struct{}{}
			}
		}

		s.skipWS()
		if s.pos >= len(s.b) || s.b[s.pos] != ':' {
			return ErrInvalidJSON
		}
		s.pos++
		s.skipWS()
		if err := s.scanValue(depth); err != nil {
			return err
		}
		s.skipWS()
		if s.pos >= len(s.b) {
			return ErrInvalidJSON
		}
		if s.b[s.pos] == '}' {
			s.pos++
			return nil
		}
		if s.b[s.pos] != ',' {
			return ErrInvalidJSON
		}
		s.pos++
	}
}

func (s *strictScanner) scanArray(depth int) error {
	s.pos++ // consume '['
	s.skipWS()
	if s.pos >= len(s.b) {
		return ErrInvalidJSON
	}
	if s.b[s.pos] == ']' {
		s.pos++
		return nil
	}
	for {
		s.skipWS()
		if err := s.scanValue(depth); err != nil {
			return err
		}
		s.skipWS()
		if s.pos >= len(s.b) {
			return ErrInvalidJSON
		}
		if s.b[s.pos] == ']' {
			s.pos++
			return nil
		}
		if s.b[s.pos] != ',' {
			return ErrInvalidJSON
		}
		s.pos++
	}
}

func (s *strictScanner) scanString(needKey bool) (string, error) {
	s.pos++ // consume '"'
	start := s.pos
	hasEscape := false
	for s.pos < len(s.b) {
		c := s.b[s.pos]
		if c == '"' {
			rawSlice := s.b[start:s.pos]
			s.pos++
			if !utf8.Valid(rawSlice) {
				return "", ErrInvalidJSON
			}
			if !needKey {
				return "", nil
			}
			if !hasEscape {
				// Safe zero-copy string view scoped strictly within ValidateStrictJSONBytes!
				return unsafe.String(unsafe.SliceData(rawSlice), len(rawSlice)), nil
			}
			return unescapeValidatedJSONString(rawSlice)
		}
		if c < 0x20 {
			return "", ErrInvalidJSON
		}
		if c == '\\' {
			hasEscape = true
			s.pos++
			if s.pos >= len(s.b) {
				return "", ErrInvalidJSON
			}
			esc := s.b[s.pos]
			switch esc {
			case '"', '\\', '/', 'b', 'f', 'n', 'r', 't':
				s.pos++
			case 'u':
				s.pos++
				u1, err := s.readHex4()
				if err != nil {
					return "", err
				}
				if u1 >= 0xDC00 && u1 <= 0xDFFF {
					return "", ErrInvalidJSON // lone low surrogate
				}
				if u1 >= 0xD800 && u1 <= 0xDBFF {
					if s.pos+2 > len(s.b) || s.b[s.pos] != '\\' || s.b[s.pos+1] != 'u' {
						return "", ErrInvalidJSON // lone high surrogate
					}
					s.pos += 2
					u2, err := s.readHex4()
					if err != nil || u2 < 0xDC00 || u2 > 0xDFFF {
						return "", ErrInvalidJSON
					}
				}
			default:
				return "", ErrInvalidJSON
			}
			continue
		}
		s.pos++
	}
	return "", ErrInvalidJSON
}

func (s *strictScanner) readHex4() (uint16, error) {
	if s.pos+4 > len(s.b) {
		return 0, ErrInvalidJSON
	}
	var val uint16
	for i := 0; i < 4; i++ {
		c := s.b[s.pos+i]
		val <<= 4
		switch {
		case c >= '0' && c <= '9':
			val |= uint16(c - '0')
		case c >= 'a' && c <= 'f':
			val |= uint16(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			val |= uint16(c - 'A' + 10)
		default:
			return 0, ErrInvalidJSON
		}
	}
	s.pos += 4
	return val, nil
}

func unescapeValidatedJSONString(escaped []byte) (string, error) {
	quoted := make([]byte, 0, len(escaped)+2)
	quoted = append(quoted, '"')
	quoted = append(quoted, escaped...)
	quoted = append(quoted, '"')
	var out string
	if err := json.Unmarshal(quoted, &out); err != nil {
		return "", ErrInvalidJSON
	}
	return out, nil
}

func (s *strictScanner) scanNumber() error {
	start := s.pos
	if s.b[s.pos] == '-' {
		s.pos++
		if s.pos >= len(s.b) {
			return ErrInvalidJSON
		}
	}
	if s.b[s.pos] == '0' {
		s.pos++
		if s.pos < len(s.b) && s.b[s.pos] >= '0' && s.b[s.pos] <= '9' {
			return ErrInvalidJSON // leading zero forbidden in RFC 8259
		}
	} else if s.b[s.pos] >= '1' && s.b[s.pos] <= '9' {
		for s.pos < len(s.b) && s.b[s.pos] >= '0' && s.b[s.pos] <= '9' {
			s.pos++
		}
	} else {
		return ErrInvalidJSON
	}

	hasExpOrFrac := false
	if s.pos < len(s.b) && s.b[s.pos] == '.' {
		hasExpOrFrac = true
		s.pos++
		if s.pos >= len(s.b) || s.b[s.pos] < '0' || s.b[s.pos] > '9' {
			return ErrInvalidJSON
		}
		for s.pos < len(s.b) && s.b[s.pos] >= '0' && s.b[s.pos] <= '9' {
			s.pos++
		}
	}
	if s.pos < len(s.b) && (s.b[s.pos] == 'e' || s.b[s.pos] == 'E') {
		hasExpOrFrac = true
		s.pos++
		if s.pos < len(s.b) && (s.b[s.pos] == '+' || s.b[s.pos] == '-') {
			s.pos++
		}
		if s.pos >= len(s.b) || s.b[s.pos] < '0' || s.b[s.pos] > '9' {
			return ErrInvalidJSON
		}
		for s.pos < len(s.b) && s.b[s.pos] >= '0' && s.b[s.pos] <= '9' {
			s.pos++
		}
	}
	if hasExpOrFrac {
		numSlice := s.b[start:s.pos]
		numStr := unsafe.String(unsafe.SliceData(numSlice), len(numSlice))
		f64, err := strconv.ParseFloat(numStr, 64)
		if err != nil || math.IsNaN(f64) || math.IsInf(f64, 0) {
			return ErrNonFiniteFloat
		}
	}
	return nil
}

// ParseStrictJSON parses UTF-8 JSON bytes while rejecting duplicate keys at any depth,
// trailing tokens, and non-finite numbers.
func ParseStrictJSON(raw []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	val, err := parseValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, ErrInvalidJSON
	}
	return val, nil
}

func parseValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	return parseToken(dec, tok)
}

func parseToken(dec *json.Decoder, tok json.Token) (any, error) {
	switch v := tok.(type) {
	case json.Delim:
		switch v {
		case '{':
			obj := make(map[string]any)
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, fmt.Errorf("%w: %v", ErrInvalidJSON, err)
				}
				key, ok := kt.(string)
				if !ok {
					return nil, ErrInvalidJSON
				}
				if _, exists := obj[key]; exists {
					return nil, fmt.Errorf("%w: %q", ErrDuplicateKey, key)
				}
				child, err := parseValue(dec)
				if err != nil {
					return nil, err
				}
				obj[key] = child
			}
			endTok, err := dec.Token()
			if err != nil || endTok != json.Delim('}') {
				return nil, ErrInvalidJSON
			}
			return obj, nil
		case '[':
			arr := make([]any, 0)
			for dec.More() {
				elem, err := parseValue(dec)
				if err != nil {
					return nil, err
				}
				arr = append(arr, elem)
			}
			endTok, err := dec.Token()
			if err != nil || endTok != json.Delim(']') {
				return nil, ErrInvalidJSON
			}
			return arr, nil
		default:
			return nil, ErrInvalidJSON
		}
	case json.Number:
		s := v.String()
		if !strings.ContainsAny(s, ".eE") {
			if i64, err := strconv.ParseInt(s, 10, 64); err == nil {
				return i64, nil
			}
		}
		f64, err := strconv.ParseFloat(s, 64)
		if err != nil || math.IsNaN(f64) || math.IsInf(f64, 0) {
			return nil, ErrNonFiniteFloat
		}
		return f64, nil
	case string, bool, nil:
		return v, nil
	default:
		return nil, ErrInvalidJSON
	}
}

// ValidateStrictValue recursively checks an in-memory Go value for NaN/Infinity before serialization.
func ValidateStrictValue(v any) error {
	if v == nil {
		return nil
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Pointer, reflect.Interface:
		if rv.IsNil() {
			return nil
		}
		return ValidateStrictValue(rv.Elem().Interface())
	case reflect.Float32, reflect.Float64:
		f := rv.Float()
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return ErrNonFiniteFloat
		}
		return nil
	case reflect.Slice, reflect.Array:
		for i := 0; i < rv.Len(); i++ {
			if err := ValidateStrictValue(rv.Index(i).Interface()); err != nil {
				return err
			}
		}
		return nil
	case reflect.Map:
		iter := rv.MapRange()
		for iter.Next() {
			if err := ValidateStrictValue(iter.Value().Interface()); err != nil {
				return err
			}
		}
		return nil
	case reflect.Struct:
		for i := 0; i < rv.NumField(); i++ {
			if rv.Type().Field(i).IsExported() {
				if err := ValidateStrictValue(rv.Field(i).Interface()); err != nil {
					return err
				}
			}
		}
		return nil
	default:
		return nil
	}
}

func isPureJSONAST(v any) bool {
	switch val := v.(type) {
	case nil, bool, string, int, int64, float64:
		return true
	case []any:
		for _, item := range val {
			if !isPureJSONAST(item) {
				return false
			}
		}
		return true
	case map[string]any:
		for _, child := range val {
			if !isPureJSONAST(child) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

// CanonicalizeUTF8 converts any JSON-serializable Go value into RFC 8785 (JCS) canonical UTF-8 bytes.
func CanonicalizeUTF8(value any) ([]byte, error) {
	if isPureJSONAST(value) {
		var buf bytes.Buffer
		if err := writeCanonical(&buf, value); err != nil {
			return nil, err
		}
		return buf.Bytes(), nil
	}
	if err := ValidateStrictValue(value); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	normalized, err := ParseStrictJSON(raw)
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	if err := writeCanonical(&buf, normalized); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// DigestSHA256 computes jcs_sha256(domainTag, value) -> "sha256:<hex>".
func DigestSHA256(domainTag string, value any) (string, error) {
	canon, err := CanonicalizeUTF8(value)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	h.Write([]byte(domainTag))
	h.Write([]byte{0})
	h.Write(canon)
	return "sha256:" + hex.EncodeToString(h.Sum(nil)), nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch val := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if val {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case string:
		writeCanonicalString(buf, val)
	case int:
		buf.WriteString(strconv.Itoa(val))
	case int64:
		buf.WriteString(strconv.FormatInt(val, 10))
	case float64:
		s, err := formatRFC8785Float(val)
		if err != nil {
			return err
		}
		buf.WriteString(s)
	case []any:
		buf.WriteByte('[')
		for i, item := range val {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, item); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			return lessUTF16(keys[i], keys[j])
		})
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeCanonicalString(buf, k)
			buf.WriteByte(':')
			if err := writeCanonical(buf, val[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("unsupported canonical value type %T", v)
	}
	return nil
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

func lessUTF16(a, b string) bool {
	if isASCII(a) && isASCII(b) {
		return a < b
	}
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

func writeCanonicalString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r >= 0 && r <= 0x1F {
				buf.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}

func formatRFC8785Float(f float64) (string, error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", ErrNonFiniteFloat
	}
	if f == 0 {
		return "0", nil
	}
	abs := math.Abs(f)
	if abs >= 1e-6 && abs < 1e21 {
		if math.Trunc(f) == f && abs <= 9007199254740991 {
			return strconv.FormatInt(int64(f), 10), nil
		}
		s := strconv.FormatFloat(f, 'f', -1, 64)
		if strings.Contains(s, ".") {
			s = strings.TrimRight(s, "0")
			s = strings.TrimRight(s, ".")
		}
		return s, nil
	}
	// Scientific notation per ECMAScript / RFC 8785: lowercase 'e', explicit '+' or '-' without leading zero in exponent.
	s := strconv.FormatFloat(f, 'e', -1, 64)
	parts := strings.SplitN(s, "e", 2)
	mant := parts[0]
	if strings.Contains(mant, ".") {
		mant = strings.TrimRight(mant, "0")
		mant = strings.TrimRight(mant, ".")
	}
	expInt, _ := strconv.Atoi(parts[1])
	if expInt >= 0 {
		return fmt.Sprintf("%se+%d", mant, expInt), nil
	}
	return fmt.Sprintf("%se%d", mant, expInt), nil
}
