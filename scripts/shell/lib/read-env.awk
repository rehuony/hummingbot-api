# Parse the literal, single-line operational subset of dotenv without execution.
# Other application values are left to python-dotenv; multiline input is rejected
# so a continuation cannot masquerade as an operational assignment.
function fail(key) {
    printf "Error: unsupported .env syntax for %s at line %d.\n", key, NR > "/dev/stderr"
    failed = 1
    exit 1
}
{
    sub(/\r$/, "")
    line = $0
    sub(/^[ \t]+/, "", line)
    if (line == "" || line ~ /^#/) next
    sub(/^export[ \t]+/, "", line)
    if (line !~ /^[A-Za-z_][A-Za-z_0-9]*[ \t]*=/) fail("assignment")
    key = line
    sub(/[ \t]*=.*/, "", key)
    value = substr(line, index(line, "=") + 1)
    sub(/^[ \t]+/, "", value)
    sub(/[ \t]+$/, "", value)
    quote = substr(value, 1, 1)
    if (quote == "\"" || quote == "\047") {
        closing = 0
        escaped = 0
        for (i = 2; i <= length(value); i++) {
            c = substr(value, i, 1)
            if (!escaped && c == quote) { closing = i; break }
            if (!escaped && c == "\\") escaped = 1
            else escaped = 0
        }
        if (!closing) fail(key)
        suffix = substr(value, closing + 1)
        if (suffix !~ /^[ \t]*(#.*)?$/) fail(key)
        value = substr(value, 2, closing - 2)
    } else {
        sub(/[ \t]+#.*/, "", value)
        sub(/[ \t]+$/, "", value)
    }
    if (key == requested) {
        if (value ~ /[$`\\]/ || value ~ /[[:cntrl:]]/) fail(key)
        result = value
        found = 1
    }
}
END {
    if (!failed && found) printf "%s", result
    if (!failed && !found) exit 2
}
