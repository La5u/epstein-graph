"""Iterate top-level JSON arrays/objects without retaining a whole graph/lookup."""
import json


def items(path, chunk_size=65536):
    decoder = json.JSONDecoder()
    with open(path) as stream:
        buffer, position, eof = '', 0, False

        def fill():
            nonlocal buffer, position, eof
            block = stream.read(chunk_size)
            buffer = buffer[position:] + block
            position = 0
            eof = not block

        def whitespace():
            nonlocal position
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer) or eof:
                    return
                fill()

        def value():
            nonlocal position
            whitespace()
            while True:
                try:
                    result, end = decoder.raw_decode(buffer, position)
                    # A number can end at a chunk boundary before its last digit.
                    if end == len(buffer) and not eof:
                        fill()
                        continue
                    position = end
                    return result
                except json.JSONDecodeError:
                    if eof:
                        raise
                    fill()

        whitespace()
        if not buffer or buffer[position] not in '[{':
            raise ValueError('Expected a top-level JSON array or object')
        is_object = buffer[position] == '{'
        closing = '}' if is_object else ']'
        position += 1
        whitespace()
        if position < len(buffer) and buffer[position] == closing:
            return
        while True:
            key = value() if is_object else None
            if is_object:
                whitespace()
                if buffer[position:position + 1] != ':':
                    raise ValueError('Expected JSON object colon')
                position += 1
            result = value()
            yield (key, result) if is_object else result
            whitespace()
            separator = buffer[position:position + 1]
            if separator == closing:
                position += 1
                whitespace()
                if position < len(buffer):
                    raise ValueError('Trailing JSON data')
                return
            if separator != ',':
                raise ValueError('Expected JSON item separator')
            position += 1
