import SwiftUI

/// Renders the cleaned pane text as Apple-Notes-style formatted content,
/// recognising the same block types as the web's `mdParse` in
/// `/dashboard/clean`:
///   - `# header` / `##` through `######`
///   - ` ``` fenced ``` ` code blocks
///   - markdown tables `| a | b |`
///   - box-drawing tables `│ a │ b │` (Claude's native)
///   - bulleted + numbered lists, including `[ ]` / `[x]` task checkboxes
///   - blockquotes (`> text`)
///   - Claude tool markers (`⏺` and `⎿`)
///   - inline: **bold**, *italic*, `code`, ~~del~~, [link]() via `AttributedString(markdown:)`
struct PaneNotesView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                render(block)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    // MARK: - Blocks

    private enum Block {
        case heading(level: Int, text: String)
        case bullet(text: String, indent: Int, checkbox: Checkbox?)
        case numbered(text: String, indent: Int)
        case toolUse(text: String)
        case toolResult(text: String)
        case code(lines: [String])
        case table(headers: [String], rows: [[String]])
        case blockquote(text: String)
        case paragraph(text: String)
        case divider
        case empty
    }

    private enum Checkbox { case unchecked, checked }

    private var blocks: [Block] { parse(text) }

    // MARK: - Parser

    private func parse(_ input: String) -> [Block] {
        var out: [Block] = []
        let lines = input.components(separatedBy: "\n")
        var i = 0
        while i < lines.count {
            let line = lines[i]
            let t = line.trimmingCharacters(in: .whitespaces)

            // code fence
            if t.hasPrefix("```") {
                i += 1
                var buf: [String] = []
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    buf.append(lines[i])
                    i += 1
                }
                if i < lines.count { i += 1 } // consume closing fence
                out.append(.code(lines: buf))
                continue
            }

            // divider
            if t == "---" || t == "___" || t == "***" {
                out.append(.divider); i += 1; continue
            }

            // heading
            if let h = headingMatch(t) {
                out.append(.heading(level: h.level, text: h.text))
                i += 1; continue
            }

            // box-drawing table  (│ cell │ cell │)
            if isBoxRow(t) {
                let (consumed, table) = parseBoxTable(lines, from: i)
                out.append(table)
                i += consumed
                continue
            }

            // markdown table (| cell | cell |)
            if isPipeRow(t) {
                let (consumed, table) = parsePipeTable(lines, from: i)
                out.append(table)
                i += consumed
                continue
            }

            // blockquote
            if t.hasPrefix(">") {
                var buf: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    let t2 = lines[i].trimmingCharacters(in: .whitespaces)
                    buf.append(String(t2.dropFirst(1)).trimmingCharacters(in: .whitespaces))
                    i += 1
                }
                out.append(.blockquote(text: buf.joined(separator: " ")))
                continue
            }

            // tool markers (Claude transcript)
            if let tu = claudeToolUse(line) { out.append(.toolUse(text: tu)); i += 1; continue }
            if let tr = claudeToolResult(line) { out.append(.toolResult(text: tr)); i += 1; continue }

            // bullets / numbered lists (with optional checkbox)
            if let b = bulletMatch(line) {
                out.append(.bullet(text: b.text, indent: b.indent, checkbox: b.checkbox))
                i += 1; continue
            }
            if let n = numberedMatch(line) {
                out.append(.numbered(text: n.text, indent: n.indent))
                i += 1; continue
            }

            // blank
            if t.isEmpty {
                out.append(.empty); i += 1; continue
            }

            // paragraph
            out.append(.paragraph(text: line))
            i += 1
        }
        return collapse(out)
    }

    /// Collapse runs of empty blocks to one; trim trailing empties.
    private func collapse(_ blocks: [Block]) -> [Block] {
        var out: [Block] = []
        var lastWasEmpty = false
        for b in blocks {
            if case .empty = b {
                if !lastWasEmpty { out.append(.empty) }
                lastWasEmpty = true
            } else {
                out.append(b)
                lastWasEmpty = false
            }
        }
        while case .empty = out.last { out.removeLast() }
        return out
    }

    // MARK: - Line recognisers

    private func headingMatch(_ t: String) -> (level: Int, text: String)? {
        guard t.hasPrefix("#") else { return nil }
        var level = 0
        for ch in t {
            if ch == "#" { level += 1 } else { break }
        }
        guard level >= 1 && level <= 6, t.dropFirst(level).first == " " else { return nil }
        return (level, String(t.dropFirst(level + 1)).trimmingCharacters(in: .whitespaces))
    }

    private func bulletMatch(_ line: String) -> (text: String, indent: Int, checkbox: Checkbox?)? {
        let indent = leadingSpaces(line)
        let s = line.dropFirst(indent)
        guard s.hasPrefix("- ") || s.hasPrefix("* ") || s.hasPrefix("+ ") else { return nil }
        var body = String(s.dropFirst(2))
        var box: Checkbox? = nil
        if body.hasPrefix("[ ] ") { box = .unchecked; body = String(body.dropFirst(4)) }
        else if body.lowercased().hasPrefix("[x] ") { box = .checked; body = String(body.dropFirst(4)) }
        return (body, indent / 2, box)
    }

    private func numberedMatch(_ line: String) -> (text: String, indent: Int)? {
        let indent = leadingSpaces(line)
        let s = line.dropFirst(indent)
        guard let dot = s.firstIndex(of: "."),
              s[s.startIndex..<dot].allSatisfy({ $0.isNumber }),
              !s[s.startIndex..<dot].isEmpty,
              s.index(after: dot) < s.endIndex,
              s[s.index(after: dot)] == " "
        else { return nil }
        return (String(s[s.index(dot, offsetBy: 2)...]), indent / 2)
    }

    private func leadingSpaces(_ line: String) -> Int {
        var n = 0
        for ch in line { if ch == " " { n += 1 } else { break } }
        return n
    }

    private func claudeToolUse(_ line: String) -> String? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.hasPrefix("⏺") else { return nil }
        return String(t.dropFirst(1).drop(while: { $0 == " " }))
    }

    private func claudeToolResult(_ line: String) -> String? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.hasPrefix("⎿") else { return nil }
        return String(t.dropFirst(1).drop(while: { $0 == " " }))
    }

    // MARK: - Tables

    private func isPipeRow(_ t: String) -> Bool {
        t.count > 2 && t.first == "|" && t.last == "|"
    }

    private func isPipeSeparator(_ t: String) -> Bool {
        guard t.contains("|") else { return false }
        let stripped = t.replacingOccurrences(of: "[|:\\- ]", with: "", options: .regularExpression)
        return stripped.isEmpty && t.count > 1
    }

    private func parsePipeTable(_ lines: [String], from start: Int) -> (consumed: Int, block: Block) {
        var raw: [String] = []
        var i = start
        while i < lines.count {
            let t = lines[i].trimmingCharacters(in: .whitespaces)
            if isPipeRow(t) || t.isEmpty {
                if !t.isEmpty { raw.append(t) }
                i += 1
            } else { break }
        }
        let dataRows = raw.filter { !isPipeSeparator($0) }
        let parsed = dataRows.map { row -> [String] in
            row.dropFirst().dropLast()
                .split(separator: "|", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces) }
        }
        let headers = parsed.first ?? []
        let body = Array(parsed.dropFirst())
        return (i - start, .table(headers: headers, rows: body))
    }

    private static let boxCell: Character = "│"

    private func isBoxRow(_ t: String) -> Bool {
        guard t.contains(Self.boxCell) else { return false }
        return t.split(separator: Self.boxCell).contains { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    private func parseBoxTable(_ lines: [String], from start: Int) -> (consumed: Int, block: Block) {
        var raw: [String] = []
        var i = start
        while i < lines.count {
            let t = lines[i].trimmingCharacters(in: .whitespaces)
            if isBoxRow(t) || t.isEmpty {
                if !t.isEmpty { raw.append(t) }
                i += 1
            } else { break }
        }
        let parsed = raw.map { row -> [String] in
            let parts = row.split(separator: Self.boxCell, omittingEmptySubsequences: false).map(String.init)
            // drop leading/trailing empty pieces caused by │ at both ends
            let core = parts.dropFirst().dropLast()
            return core.map { $0.trimmingCharacters(in: .whitespaces) }
        }
        let headers = parsed.first ?? []
        let body = Array(parsed.dropFirst())
        return (i - start, .table(headers: headers, rows: body))
    }

    // MARK: - Render

    @ViewBuilder
    private func render(_ block: Block) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(inline(text))
                .font(headingFont(level))
                .padding(.top, level <= 2 ? 6 : 2)

        case .bullet(let text, let indent, let checkbox):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if let box = checkbox {
                    Image(systemName: box == .checked ? "checkmark.square.fill" : "square")
                        .font(.caption)
                        .foregroundStyle(box == .checked ? Color.accentColor : .secondary)
                } else {
                    Text("•").foregroundStyle(.secondary)
                }
                Text(inline(text))
                    .textSelection(.enabled)
                    .strikethrough(checkbox == .checked, color: .secondary)
                    .foregroundStyle(checkbox == .checked ? .secondary : .primary)
            }
            .padding(.leading, CGFloat(indent) * 14)

        case .numbered(let text, let indent):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("›").foregroundStyle(.secondary)
                Text(inline(text)).textSelection(.enabled)
            }
            .padding(.leading, CGFloat(indent) * 14)

        case .toolUse(let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Circle().fill(Color.accentColor).frame(width: 6, height: 6)
                Text(inline(text))
                    .font(.callout)
                    .textSelection(.enabled)
            }

        case .toolResult(let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("↳").foregroundStyle(.tertiary).font(.footnote)
                Text(inline(text))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            .padding(.leading, 10)

        case .code(let lines):
            Text(lines.joined(separator: "\n"))
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color.black.opacity(0.05),
                            in: RoundedRectangle(cornerRadius: 6, style: .continuous))

        case .table(let headers, let rows):
            TableBlock(headers: headers, rows: rows)

        case .blockquote(let text):
            HStack(alignment: .top, spacing: 8) {
                Rectangle()
                    .fill(Color.secondary.opacity(0.5))
                    .frame(width: 3)
                Text(inline(text))
                    .italic()
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 2)

        case .paragraph(let text):
            Text(inline(text))
                .textSelection(.enabled)

        case .divider:
            Divider().padding(.vertical, 4)

        case .empty:
            Spacer().frame(height: 6)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title2.weight(.bold)
        case 2: return .title3.weight(.semibold)
        case 3: return .headline
        case 4: return .subheadline.weight(.semibold)
        default: return .subheadline.weight(.medium)
        }
    }

    private func inline(_ s: String) -> AttributedString {
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .inlineOnlyPreservingWhitespace
        if let attr = try? AttributedString(markdown: s, options: options) {
            return attr
        }
        return AttributedString(s)
    }
}

// MARK: - Table renderer

private struct TableBlock: View {
    let headers: [String]
    let rows: [[String]]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                if !headers.isEmpty {
                    row(headers, isHeader: true)
                    Divider()
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { idx, r in
                    row(r, isHeader: false)
                        .background(idx.isMultiple(of: 2)
                                    ? Color.secondary.opacity(0.06)
                                    : Color.clear)
                    if idx < rows.count - 1 { Divider().opacity(0.3) }
                }
            }
            .padding(.vertical, 2)
            .background(Color.secondary.opacity(0.04),
                        in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        }
    }

    @ViewBuilder
    private func row(_ cells: [String], isHeader: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(cells.enumerated()), id: \.offset) { _, c in
                Text(inline(c))
                    .font(isHeader ? .caption.weight(.semibold) : .caption)
                    .foregroundStyle(isHeader ? .primary : .primary)
                    .frame(minWidth: 80, maxWidth: 220, alignment: .leading)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
            }
        }
    }

    private func inline(_ s: String) -> AttributedString {
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .inlineOnlyPreservingWhitespace
        return (try? AttributedString(markdown: s, options: options)) ?? AttributedString(s)
    }
}
