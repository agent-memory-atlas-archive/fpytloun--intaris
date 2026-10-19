"""Bounded quote-aware shell observations; never an authorization parser."""


def substitutions(command: str) -> tuple[str, list[str]]:
    """Mask active substitutions, retaining outer executable and quoted literals."""
    output, nested = [], []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            output.append(command[index : index + 2])
            index += 2
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            output.append(char)
            index += 1
            continue
        if quote != "'" and (command.startswith("$(", index) or char == "`"):
            start = index + (2 if char == "$" else 1)
            cursor, depth, inner_quote = start, 1, None
            while cursor < len(command):
                current = command[cursor]
                if current == "\\" and inner_quote != "'":
                    cursor += 2
                    continue
                if current in ("'", '"'):
                    if inner_quote == current:
                        inner_quote = None
                    elif inner_quote is None:
                        inner_quote = current
                elif inner_quote is None:
                    if char == "`" and current == "`":
                        depth = 0
                    elif char == "$":
                        if current == "(":
                            depth += 1
                        elif current == ")":
                            depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            if depth == 0:
                nested.append(command[start:cursor])
                output.append("SUBSTITUTION")
                index = cursor + 1
                continue
        output.append(char)
        index += 1
    return "".join(output), nested


def unquoted_redirect(command: str) -> bool:
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == ">" and quote is None:
            if command[index + 1 : index + 2] == "&":
                # Descriptor duplication is not a persistent file mutation.
                # The enclosing shell command still requires assessment.
                index += 2
                continue
            start = index + 2 if command[index + 1 : index + 2] == ">" else index + 1
            target = command[start:].lstrip()
            end = len("/dev/null")
            if target.startswith("/dev/null") and (
                len(target) == end
                or not (target[end].isalnum() or target[end] in "._/-")
            ):
                # Discarding output writes nothing persistent.
                index += 1
                continue
            return True
        index += 1
    return False
