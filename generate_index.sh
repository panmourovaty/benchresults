#!/bin/bash

OUTPUT_FILE="index.html"

echo "<!DOCTYPE html>
<html lang='en'>
<head>
    <meta charset='UTF-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>Pan Mourovaty's benchmarks</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.6; }
        h1 { border-bottom: 1px solid #eaeaea; padding-bottom: 0.5rem; }
        ul { list-style-type: none; padding: 0; }
        li { margin: 0.5rem 0; border-bottom: 1px solid #f0f0f0; }
        a { text-decoration: none; color: #0366d6; display: block; padding: 0.5rem; }
        a:hover { background-color: #f6f8fa; border-radius: 4px; }
        .footer { margin-top: 2rem; color: #666; font-size: 0.8rem; }
    </style>
</head>
<body>
    <h1>Pan Mourovaty's benchmarks</h1>
    <ul>" > "$OUTPUT_FILE"
find . -type f | \
grep -v "\.git" | \
grep -v "$0" | \
grep -v "$OUTPUT_FILE" | \
sort | \
while read -r filepath; do
    cleanpath="${filepath#./}"
    echo "        <li><a href=\"$cleanpath\">$cleanpath</a></li>" >> "$OUTPUT_FILE"
done

echo "    </ul>
    <div class='footer'><a href="https://github.com/panmourovaty/benchresults">Source at GitHub</a></div>
</body>
</html>" >> "$OUTPUT_FILE"

echo "Directory listing generated at $OUTPUT_FILE"
