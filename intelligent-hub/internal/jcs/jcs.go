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
)

var (
	ErrDuplicateKey   = errors.New("duplicate JSON object key")
	ErrNonFiniteFloat = errors.New("non-finite float (NaN or Infinity) is forbidden")
	ErrInvalidJSON    = errors.New("invalid strict RFC 8259 JSON")
)

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
