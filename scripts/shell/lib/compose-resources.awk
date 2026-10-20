# Read resource identity from Docker Compose's canonical YAML, not user YAML.
# Only top-level resources and their direct name/external fields are relevant.
function invalid() {
    print "Error: cannot read resolved Compose resource identity safely." > "/dev/stderr"
    failed = 1
    exit 1
}
function scalar(value, quote) {
    quote = substr(value, 1, 1)
    if (quote == "\"" || quote == "\047") {
        if (substr(value, length(value), 1) != quote) invalid()
        value = substr(value, 2, length(value) - 2)
        if (index(value, quote)) invalid()
    }
    if (value == "" || value ~ /[\\[:cntrl:]]/) invalid()
    return value
}
function emit() {
    if (key == "") return
    if (name == "") invalid()
    printf "%s\t%s\t%s\t%s\n", kind, key, name, external
    key = ""
}
/^[^ ]/ {
    emit()
    kind = ""
    if ($0 == "volumes:") kind = "volume"
    if ($0 == "networks:") kind = "network"
    next
}
kind != "" && /^  [^ ]/ {
    emit()
    if ($0 !~ /:$/) invalid()
    key = scalar(substr($0, 3, length($0) - 3))
    name = ""
    external = "false"
    next
}
kind != "" && /^    name: / {
    name = scalar(substr($0, 11))
    next
}
kind != "" && /^    external: / {
    external = substr($0, 15)
    if (external != "true" && external != "false") invalid()
}
END {
    if (!failed) emit()
}
