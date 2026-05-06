BEGIN {
    repl = sprintf("%c", 92)
}

function trim(s) {
    sub(/^[ \t\r\n]+/, "", s)
    sub(/[ \t\r\n]+$/, "", s)
    return s
}

function normalize(line) {
    line = trim(line)
    if (length(line) >= 2 && substr(line, 1, 1) == "\"" && substr(line, length(line), 1) == "\"") {
        line = substr(line, 2, length(line) - 2)
        line = trim(line)
    }
    return line
}

function is_absolute(s) {
    return (s ~ /^[A-Za-z]:[\\\/]/) || (s ~ /^\\\\/) || (s ~ /^\//)
}

function canon_path(s) {
    gsub(/\//, repl, s)
    return s
}

function path_parent(s, t) {
    t = canon_path(s)
    sub(/[\\\/][^\\\/]+$/, "", t)
    if (t == s) {
        return ""
    }
    return t
}

function path_name(s, t) {
    t = canon_path(s)
    sub(/^.*[\\\/]/, "", t)
    return t
}

{
    line = normalize($0)
    if (line == "") {
        next
    }
    if (!seen[line]++) {
        items[++count] = line
    }
}

END {
    base_parent = ""
    base_name = ""

    for (i = 1; i <= count; i++) {
        item = items[i]
        if (!is_absolute(item)) {
            continue
        }
        parent = path_parent(item)
        if (parent == "") {
            continue
        }
        if (base_parent == "") {
            base_parent = parent
            base_name = path_name(parent)
            continue
        }
        if (tolower(canon_path(parent)) != tolower(canon_path(base_parent))) {
            print "All absolute paths must share the same parent directory." > "/dev/stderr"
            exit 1
        }
    }

    if (base_parent == "" || base_name == "") {
        print "No absolute path found; cannot determine the common parent directory." > "/dev/stderr"
        exit 1
    }

    bak_list = ""
    delete name_seen
    for (i = 1; i <= count; i++) {
        item = items[i]
        if (is_absolute(item)) {
            parent = path_parent(item)
            if (tolower(canon_path(parent)) != tolower(canon_path(base_parent))) {
                print "All absolute paths must share the same parent directory." > "/dev/stderr"
                exit 1
            }
        }
        name = path_name(item)
        if (name != "" && !name_seen[name]++) {
            if (bak_list != "") {
                bak_list = bak_list ","
            }
            bak_list = bak_list "!" name
        }
    }

    print "[" base_name "]"
    print "sources = " base_parent
    print "target = .\\target_" base_name
    print "ignore=*," bak_list
}
