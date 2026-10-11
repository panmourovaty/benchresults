
#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="index.html"
shopt -s nullglob dotglob

# Escape text for safe HTML output
html_escape() {
    local text="$1"
    text=${text//&/\&amp;}
    text=${text//</\&lt;}
    text=${text//>/\&gt;}
    text=${text//\"/\&quot;}
    text=${text//\'/\&#39;}
    printf '%s' "$text"
}

# URL-encode filenames while preserving directory separators
url_encode() {
    local LC_ALL=C
    local value="$1" char i

    for ((i = 0; i < ${#value}; i++)); do
        char="${value:i:1}"
        case "$char" in
            [a-zA-Z0-9.~_/-])
                printf '%s' "$char"
                ;;
            *)
                printf '%%%02X' "'$char"
                ;;
        esac
    done
}

# Recursively generate the folder tree
render_dir() {
    local dir="$1" path name relative_path

    for path in "$dir"/*; do
        name="${path##*/}"

        if [[ -d "$path" ]]; then
            [[ "$name" == ".git" ]] && continue

            printf '<li class="folder">\n'
            printf '<details>\n'
            printf '<summary>📁 %s</summary>\n' \
                "$(html_escape "$name")"
            printf '<ul>\n'

            render_dir "$path"

            printf '</ul>\n'
            printf '</details>\n'
            printf '</li>\n'

        elif [[ -f "$path" ]]; then
            # Exclude generated index and this script
            [[ "$path" == "./$OUTPUT_FILE" || "$path" -ef "$0" ]] && continue

            relative_path="${path#./}"

            printf '<li><a href="%s">📄 %s</a></li>\n' \
                "$(url_encode "$relative_path")" \
                "$(html_escape "$name")"
        fi
    done
}

# Generate the HTML page
{
    cat <<'HTML'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pan Mourovaty's benchmarks</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont,
                'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            padding: 2rem;
            max-width: 800px;
            margin: 0 auto;
            line-height: 1.6;
        }

        h1 {
            border-bottom: 1px solid #eaeaea;
            padding-bottom: 0.5rem;
        }

        ul {
            list-style-type: none;
            padding: 0;
            margin: 0;
        }

        li {
            margin: 0.3rem 0;
        }

        ul ul {
            margin-left: 1rem;
            padding-left: 1rem;
            border-left: 1px solid #eaeaea;
        }

        summary, a {
            padding: 0.4rem 0.5rem;
            border-radius: 4px;
        }

        summary {
            display: list-item;
            cursor: pointer;
            font-weight: 600;
        }

        a {
            display: block;
            text-decoration: none;
            color: #0366d6;
        }

        summary:hover, a:hover {
            background-color: #f6f8fa;
        }

        .footer {
            margin-top: 2rem;
            color: #666;
            font-size: 0.8rem;
        }
    </style>
</head>
<body>
    <h1>Pan Mourovaty's benchmarks</h1>
    <ul>
HTML

    render_dir "."

    cat <<'HTML'
    </ul>
    <div class="footer">
        <a href="https://github.com/panmourovaty/benchresults">
            Source at GitHub
        </a>
    </div>
</body>
</html>
HTML
} > "$OUTPUT_FILE"

printf 'Directory listing generated at %s\n' "$OUTPUT_FILE"
