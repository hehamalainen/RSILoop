from typing import Any


def parse_json(s: str) -> Any:
    class Parser:
        def __init__(self, text: str) -> None:
            self.text = text
            self.pos = 0
            self.length = len(text)

        def parse(self) -> Any:
            self.skip_whitespace()
            value = self.parse_value()
            self.skip_whitespace()
            if self.pos != self.length:
                self.error("Unexpected trailing data")
            return value

        def skip_whitespace(self) -> None:
            while self.pos < self.length and self.text[self.pos] in " \t\r\n":
                self.pos += 1

        def parse_value(self) -> Any:
            self.skip_whitespace()
            if self.pos >= self.length:
                self.error("Expected a JSON value")

            char = self.text[self.pos]
            if char == "{":
                return self.parse_object()
            if char == "[":
                return self.parse_array()
            if char == '"':
                return self.parse_string()
            if char == "-" or char.isdigit():
                return self.parse_number()
            if self.text.startswith("true", self.pos):
                self.pos += 4
                return True
            if self.text.startswith("false", self.pos):
                self.pos += 5
                return False
            if self.text.startswith("null", self.pos):
                self.pos += 4
                return None

            self.error("Invalid JSON value")

        def parse_object(self) -> dict[str, Any]:
            result: dict[str, Any] = {}
            self.pos += 1
            self.skip_whitespace()

            if self.consume("}"):
                return result

            while True:
                self.skip_whitespace()
                if self.pos >= self.length or self.text[self.pos] != '"':
                    self.error("Object keys must be strings")

                key = self.parse_string()
                self.skip_whitespace()
                if not self.consume(":"):
                    self.error("Expected ':' after object key")

                result[key] = self.parse_value()
                self.skip_whitespace()

                if self.consume("}"):
                    return result
                if not self.consume(","):
                    self.error("Expected ',' or '}' in object")

                self.skip_whitespace()
                if self.pos < self.length and self.text[self.pos] == "}":
                    self.error("Trailing comma in object")

        def parse_array(self) -> list[Any]:
            result: list[Any] = []
            self.pos += 1
            self.skip_whitespace()

            if self.consume("]"):
                return result

            while True:
                result.append(self.parse_value())
                self.skip_whitespace()

                if self.consume("]"):
                    return result
                if not self.consume(","):
                    self.error("Expected ',' or ']' in array")

                self.skip_whitespace()
                if self.pos < self.length and self.text[self.pos] == "]":
                    self.error("Trailing comma in array")

        def parse_string(self) -> str:
            self.pos += 1
            chars: list[str] = []

            while self.pos < self.length:
                char = self.text[self.pos]
                self.pos += 1

                if char == '"':
                    return "".join(chars)

                if char == "\\":
                    if self.pos >= self.length:
                        self.error("Unterminated escape sequence")

                    escape = self.text[self.pos]
                    self.pos += 1
                    escapes = {
                        '"': '"',
                        "\\": "\\",
                        "/": "/",
                        "b": "\b",
                        "f": "\f",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                    }

                    if escape in escapes:
                        chars.append(escapes[escape])
                    elif escape == "u":
                        chars.append(self.parse_unicode_escape())
                    else:
                        self.error("Invalid escape sequence")
                else:
                    if ord(char) < 0x20:
                        self.error("Unescaped control character in string")
                    chars.append(char)

            self.error("Unterminated string")

        def parse_unicode_escape(self) -> str:
            code = self.read_hex_codepoint()

            if 0xD800 <= code <= 0xDBFF:
                if not self.text.startswith("\\u", self.pos):
                    self.error("Missing low surrogate")
                self.pos += 2
                low = self.read_hex_codepoint()
                if not 0xDC00 <= low <= 0xDFFF:
                    self.error("Invalid low surrogate")
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            elif 0xDC00 <= code <= 0xDFFF:
                self.error("Unexpected low surrogate")

            return chr(code)

        def read_hex_codepoint(self) -> int:
            if self.pos + 4 > self.length:
                self.error("Incomplete Unicode escape")

            digits = self.text[self.pos:self.pos + 4]
            if any(char not in "0123456789abcdefABCDEF" for char in digits):
                self.error("Invalid Unicode escape")

            self.pos += 4
            return int(digits, 16)

        def parse_number(self) -> int | float:
            start = self.pos

            if self.consume("-") and self.pos >= self.length:
                self.error("Invalid number")

            if self.consume("0"):
                if self.pos < self.length and self.text[self.pos].isdigit():
                    self.error("Leading zeros are not allowed")
            else:
                if self.pos >= self.length or not self.text[self.pos].isdigit():
                    self.error("Invalid number")
                while self.pos < self.length and self.text[self.pos].isdigit():
                    self.pos += 1

            if self.consume("."):
                if self.pos >= self.length or not self.text[self.pos].isdigit():
                    self.error("Expected digit after decimal point")
                while self.pos < self.length and self.text[self.pos].isdigit():
                    self.pos += 1

            if self.pos < self.length and self.text[self.pos] in "eE":
                self.pos += 1
                if self.pos < self.length and self.text[self.pos] in "+-":
                    self.pos += 1
                if self.pos >= self.length or not self.text[self.pos].isdigit():
                    self.error("Expected digit in exponent")
                while self.pos < self.length and self.text[self.pos].isdigit():
                    self.pos += 1

            token = self.text[start:self.pos]
            return float(token) if any(char in token for char in ".eE") else int(token)

        def consume(self, char: str) -> bool:
            if self.pos < self.length and self.text[self.pos] == char:
                self.pos += 1
                return True
            return False

        def error(self, message: str) -> None:
            raise ValueError(f"{message} at position {self.pos}")

    if not isinstance(s, str):
        raise TypeError("parse_json() argument must be a string")

    return Parser(s).parse()
