"""Generate docs/architecture.svg: swimlanes showing where the AI, the code and the human act."""

import pathlib

W = 840
LX = 74
GAP = 18
BW = (W - LX - 4 * GAP - 4) / 5
ROW_H = 56
BOX_H = 40
FS = 11.5
LANES = [
    ("AI", "probabilistic", "#fff4e0", "#c77700"),
    ("CODE", "deterministic", "#e8f1fb", "#1f5fa8"),
    ("HUMAN", "audit gate", "#eaf6ea", "#2c7a2c"),
]
LANE_A = [
    (1, "A1 Hash, page text,\\noffsets"),
    (0, "A2-3 Locate clauses,\\nextract spec draft"),
    (1, "A4 verify_span +\\nschema checks"),
    (2, "A5 Gate 1: approve\\nspec (back-test)"),
    (1, "A6 ACTIVE spec,\\nfrozen + hashed"),
]
LANE_B = [
    (0, "B2 Extract facts\\nfrom borrower pack"),
    (1, "B3 Span check,\\ntie-outs, scale"),
    (1, "B4-5 Compute AST,\\nclassify"),
    (2, "B6 Gate 2: breach,\\nthin, 10% sample"),
    (1, "B7 Sealed,\\nhash-chained log"),
]


def build() -> str:
    out: list[str] = []
    set_h = 3 * ROW_H
    top_a, top_b = 22, 22 + set_h + 58
    height = top_b + set_h + 30
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" font-family="Helvetica,Arial,sans-serif">')
    for cid, color in (("a", "#333"), ("r", "#b00020"), ("g", "#777")):
        out.append(
            f'<defs><marker id="{cid}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
            f'<path d="M0,0 L8,4 L0,8 z" fill="{color}"/></marker></defs>'
        )
    pos: dict[tuple[str, int], tuple[float, float]] = {}

    def lane_set(tag: str, top: int, title: str, boxes: list[tuple[int, str]]) -> None:
        out.append(f'<text x="{2 if tag == "a" else W - 2}" y="{top - 8}" font-size="12.5" font-weight="bold" text-anchor="{"start" if tag == "a" else "end"}">{title}</text>')
        for i, (name, sub, bg, fg) in enumerate(LANES):
            y = top + i * ROW_H
            out.append(f'<rect x="1" y="{y}" width="{W - 2}" height="{ROW_H - 2}" fill="{bg}" stroke="{fg}"/>')
            out.append(f'<text x="6" y="{y + ROW_H / 2 - 3}" font-size="11" font-weight="bold" fill="{fg}">{name}</text>')
            out.append(f'<text x="6" y="{y + ROW_H / 2 + 10}" font-size="9" fill="{fg}">{sub}</text>')
        for i, (lane, text) in enumerate(boxes):
            x = LX + i * (BW + GAP)
            y = top + lane * ROW_H + (ROW_H - 2 - BOX_H) / 2
            fg = LANES[lane][3]
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{BW:.1f}" height="{BOX_H}" rx="4" fill="white" stroke="{fg}" stroke-width="1.6"/>')
            for j, line in enumerate(text.split("\\n")):
                out.append(f'<text x="{x + BW / 2:.1f}" y="{y + 17 + j * 14:.1f}" font-size="{FS}" text-anchor="middle">{line}</text>')
            pos[(tag, i)] = (x, y)
        for i in range(4):
            x0, y0 = pos[(tag, i)]
            x1, y1 = pos[(tag, i + 1)]
            ax, ay = x0 + BW, y0 + BOX_H / 2
            bx, by = x1, y1 + BOX_H / 2
            mx = ax + GAP / 2
            out.append(f'<path d="M{ax:.1f},{ay:.1f} L{mx:.1f},{ay:.1f} L{mx:.1f},{by:.1f} L{bx:.1f},{by:.1f}" stroke="#333" stroke-width="1.4" fill="none" marker-end="url(#a)"/>')

    lane_set("a", top_a, "LANE A: compile contract (once per agreement or amendment)", LANE_A)
    lane_set("b", top_b, "LANE B: quarterly test (every borrower, every period)", LANE_B)

    x2, y2 = pos[("a", 2)]
    x1, y1 = pos[("a", 1)]
    gy = top_a + ROW_H - 1
    out.append(
        f'<path d="M{x2 + 28:.1f},{y2:.1f} L{x2 + 28:.1f},{gy} L{x1 + BW - 28:.1f},{gy} L{x1 + BW - 28:.1f},{y1 + BOX_H:.1f}" '
        'stroke="#b00020" stroke-width="1.3" stroke-dasharray="4,3" fill="none" marker-end="url(#r)"/>'
    )
    out.append(f'<text x="{x1 + BW + 2:.1f}" y="{y1 + 12:.1f}" font-size="10" fill="#b00020">fail: error fed back, retry x2</text>')

    xa, ya = pos[("a", 4)]
    xb, yb = pos[("b", 0)]
    mid = top_a + set_h + 20
    out.append(
        f'<path d="M{xa + BW / 2:.1f},{ya + BOX_H:.1f} L{xa + BW / 2:.1f},{mid} L{xb + BW / 2:.1f},{mid} L{xb + BW / 2:.1f},{yb:.1f}" '
        'stroke="#777" stroke-width="1.3" stroke-dasharray="2,3" fill="none" marker-end="url(#g)"/>'
    )
    out.append(f'<text x="{xb + BW / 2 + 8:.1f}" y="{mid - 4}" font-size="10" fill="#555">approved, hashed spec drives every quarterly test</text>')
    out.append(
        f'<text x="{W / 2}" y="{top_b + set_h + 17}" font-size="10.5" fill="#b00020" text-anchor="middle">'
        'STALE: an amendment, restatement or revoked spec re-opens Lane A and re-queues dependent results</text>'
    )
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    (pathlib.Path(__file__).parent / "architecture.svg").write_text(build())
