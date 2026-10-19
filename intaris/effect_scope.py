"""Minimal external/irreversible boundary for intention-based permission."""

import re
from urllib.parse import urlsplit


def external_action(tool: str, args: dict, *, paths=(), name_hints=True) -> bool:
    for value in (
        *paths,
        *(args.get(key, "") for key in ("path", "file_path", "filePath")),
    ):
        path = str(value).lower()
        if re.search(r"(?:credentials|authorized_keys|/\.ssh/|/\.aws/)", path):
            return True
    words = set(re.split(r"[^a-z0-9]+", tool.lower())) if name_hints else set()
    if words & {
        "send",
        "publish",
        "payment",
        "pay",
        "purchase",
        "checkout",
        "order",
        "delete",
        "remove",
        "deploy",
        "deployment",
        "push",
        "credential",
        "password",
        "transfer",
        "book",
        "booking",
    }:
        return True
    if tool.lower() == "bash":
        return _bash_external(str(args.get("command", args.get("cmd", ""))))
    return False


_EXTERNAL_HEAD = re.compile(
    r"(?:git push|kubectl (?:apply|delete|rollout)|helm (?:upgrade|install)"
    r"|terraform (?:apply|destroy)|rm|rmdir|passwd)(?: |$)"
)
# Wrappers that execute their operand, with options taking a separate value.
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-U", "-r", "-t", "-T"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"},
    "timeout": {"-s", "-k"},
    "nice": {"-n"},
    "nohup": set(),
}
_CURL_WRITE = re.compile(r"-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s", re.I)
# curl options whose separate value is not a request target.
_CURL_VALUE_SHORT = set("dHoXuAebcwmFTErCDY")
_CURL_VALUE_LONG = {
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--data-ascii",
    "--json",
    "--header",
    "--output",
    "--request",
    "--user",
    "--user-agent",
    "--referer",
    "--cookie",
    "--cookie-jar",
    "--write-out",
    "--max-time",
    "--connect-timeout",
    "--retry",
    "--form",
    "--upload-file",
    "--cacert",
    "--cert",
    "--key",
    "--range",
    "--retry-delay",
    "--retry-max-time",
    "--limit-rate",
    "--max-filesize",
    "--speed-limit",
    "--speed-time",
}
# Options that redirect or hide the real destination.
_CURL_REROUTE_SHORT = set("xK")
_CURL_REROUTE_LONG = (
    "--proxy",
    "--preproxy",
    "--socks",
    "--resolve",
    "--connect-to",
    "--config",
)


def _local_host(host: str | None) -> bool:
    """Loopback or in-cluster Kubernetes service host."""
    host = (host or "").lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(
        (".svc", ".svc.cluster.local")
    )


def _curl_local_only(tokens: list[str]) -> bool:
    """True only when every curl request target is a local host.

    Any non-local, undeterminable or rerouted target keeps the call external.
    """
    targets = []
    # Only known value-taking options consume the next token; after an unknown
    # option the next token is a possible target, so it stays conservative.
    skip_value = False
    for token in tokens[1:]:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("--"):
            name, _, value = token.partition("=")
            if name.startswith(_CURL_REROUTE_LONG):
                return False
            if name == "--url" and value:
                targets.append(value)
            # A separate --url value is collected as a positional target.
            skip_value = "=" not in token and name in _CURL_VALUE_LONG
            continue
        if token.startswith("-") and len(token) > 1:
            # Combined short flags: the first value-taking letter consumes the
            # rest of the token, or the next token when nothing follows it.
            for index, letter in enumerate(token[1:], start=1):
                if letter in _CURL_REROUTE_SHORT:
                    return False
                if letter in _CURL_VALUE_SHORT:
                    skip_value = index == len(token) - 1
                    break
            continue
        targets.append(token)
    if not targets:
        return False
    for target in targets:
        try:
            host = urlsplit(target if "://" in target else f"http://{target}").hostname
        except ValueError:
            return False
        if not _local_host(host):
            return False
    return True


def _bash_external(command: str) -> bool:
    """Match executed command words, not text inside quoted arguments.

    Code passed to interpreters (python -c, bash -c) is left to L1's
    effect classification.
    """
    from intaris.classifier import (
        _simple_shell_segments,
        _strip_quoted_strings,
        _unwrap_read_command,
    )
    from intaris.shell_observations import substitutions

    command, nested = substitutions(command)
    if any(_bash_external(inner) for inner in nested):
        return True
    if "\n" in command:
        return any(_bash_external(line) for line in command.splitlines())
    segments = _simple_shell_segments(command)
    if not segments and command.strip():
        # Unsupported single-line syntax: conservative scan outside quotes.
        stripped = _strip_quoted_strings(command)
        return bool(
            re.search(r"(?<![-\w])(?:rm|rmdir|passwd)\s|git\s+push", stripped)
            or (re.search(r"\bcurl\b", stripped) and _CURL_WRITE.search(stripped))
        )
    for segment in segments:
        tokens = _unwrap_read_command(segment)
        # Wrappers that execute their operand (sudo rm, xargs rm, timeout 5 rm).
        while tokens and tokens[0] in _WRAPPER_VALUE_OPTIONS:
            valued = _WRAPPER_VALUE_OPTIONS[tokens[0]]
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or tokens[0][:1].isdigit()):
                # Separate option values (sudo -u root, xargs -I {}).
                tokens = tokens[2:] if tokens[0] in valued else tokens[1:]
        if not tokens:
            continue
        head = " ".join([tokens[0].rsplit("/", 1)[-1], *tokens[1:2]])
        if _EXTERNAL_HEAD.match(head):
            return True
        if (
            head.startswith("curl")
            and _CURL_WRITE.search(" ".join(tokens) + " ")
            and not _curl_local_only(tokens)
        ):
            return True
    return False


_URL_HOST = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:[^/\s'\"@]*@)?(\[[^\]\s]+\]|[^/\s'\":?#]+)", re.I
)


def external_reason(tool: str, args: dict | None) -> str:
    """Plain-words reason for an external-effect approval, naming the tool and,
    when a non-local URL is visible, its destination host.

    Display only: the host is bounded to safe hostname characters.
    """
    safe_tool = tool if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", tool or "") else ""
    subject = (
        "This bash command"
        if safe_tool == "bash"
        else f"This {safe_tool} action"
        if safe_tool
        else "This action"
    )
    command = str((args or {}).get("command", (args or {}).get("cmd", "")) or "")
    for match in _URL_HOST.finditer(command):
        host = match.group(1).strip("[]")
        if not _local_host(host) and re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
            return (
                f"{subject} sends a network request to {host}, "
                "which needs your approval."
            )
    return (
        f"{subject} may have an external or irreversible effect, "
        "which needs your approval."
    )


def requires_explicit_grant(state) -> bool:
    """Require a grant for consequential boundaries, not network transport alone."""
    return bool(
        state.value("external_or_irreversible", False)
        or set(state.value("effects", [])) & {"delete", "disclose"}
        or state.value("reversibility") == "irreversible"
        or (
            state.value("environment") == "production"
            and state.value("mutation") != "read_only"
        )
    )
