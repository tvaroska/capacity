"""Pure HTML renderer joining real `.html`/`.css`/`.js`/`.svg` assets and escaping in one place.

Takes a display-ready `PageView` (or builds one via `capacity/showcase/view.py`), contains zero
numeric literals or calculations, and escapes every text and attribute slot through `_esc`.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import quote

from capacity.contract.results import RecommendationResponse
from capacity.contract.scenario import ShowcaseReport
from capacity.showcase.view import (
    BadgeView,
    BannerView,
    CardRowView,
    CardView,
    CellView,
    ControlFieldView,
    KpiCardView,
    PageView,
    TableView,
    TabView,
    build_recommendations_page_view,
    build_showcase_page_view,
)

_ASSETS_DIR = Path(__file__).with_name("assets")
_HTML_SHELL = (_ASSETS_DIR / "workbench.html").read_text(encoding="utf-8")
_CSS_STYLES = (_ASSETS_DIR / "workbench.css").read_text(encoding="utf-8")
_JS_SCRIPT = (_ASSETS_DIR / "workbench.js").read_text(encoding="utf-8")
_FAVICON_SVG = (_ASSETS_DIR / "favicon.svg").read_text(encoding="utf-8").strip()
FAVICON_DATA_URI = "data:image/svg+xml," + quote(_FAVICON_SVG, safe=" =':/.,-")


def _esc(value: str) -> str:
    """Single HTML escape point for every text node and attribute value on the page."""
    return html.escape(value, quote=True).replace("\u2192", "&rarr;")


def _render_badge(badge: BadgeView) -> str:
    id_attr = f' id="{_esc(badge.element_id)}"' if badge.element_id else ""
    return f'<span class="badge badge-{_esc(badge.tone)}"{id_attr}>{_esc(badge.text)}</span>'


def _render_kpi_grid(kpis: tuple[KpiCardView, ...]) -> str:
    if not kpis:
        return ""
    items: list[str] = []
    for kpi in kpis:
        val_id = f' id="{_esc(kpi.value_id)}"' if kpi.value_id else ""
        cap_id = f' id="{_esc(kpi.caption_id)}"' if kpi.caption_id else ""
        items.append(
            '<div class="kpi-card">'
            f'<div class="kpi-label">{_esc(kpi.label)}</div>'
            f'<div class="kpi-value"{val_id}>{_esc(kpi.value)}</div>'
            f"{_render_badge(kpi.badge)}"
            f'<div class="kpi-caption"{cap_id}>{_esc(kpi.caption)}</div>'
            "</div>"
        )
    return f'<div class="kpi-grid">{"".join(items)}</div>'


def _render_cell(cell: CellView) -> str:
    classes: list[str] = []
    if cell.numeric:
        classes.append("num")
    if cell.tone == "success":
        classes.append("tone-success")
    cls_attr = f' class="{" ".join(classes)}"' if classes else ""
    col_attr = f' colspan="{_esc(cell.colspan)}"' if cell.colspan is not None else ""

    if cell.badge is not None:
        inner = _render_badge(cell.badge)
    elif cell.code is not None:
        inner = f"<code>{_esc(cell.code)}</code>"
    else:
        inner = _esc(cell.text)
        if cell.bold_suffix is not None:
            inner = f"{inner}<strong>{_esc(cell.bold_suffix)}</strong>"
        if cell.strong:
            inner = f"<strong>{inner}</strong>"
        if cell.note is not None:
            inner = f"{inner} <small>({_esc(cell.note)})</small>"
    return f"<td{cls_attr}{col_attr}>{inner}</td>"


def _render_table(table: TableView) -> str:
    ths = "".join(
        f'<th class="num">{_esc(h.label)}</th>' if h.numeric else f"<th>{_esc(h.label)}</th>"
        for h in table.headers
    )
    trs = "".join(f"<tr>{''.join(_render_cell(c) for c in row)}</tr>" for row in table.rows)
    return f"<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"


def _render_control(ctrl: ControlFieldView) -> str:
    fid = _esc(ctrl.field_id)
    dis = " disabled" if ctrl.disabled else ""
    if ctrl.kind == "select":
        evt = f' onchange="{_esc(ctrl.handler)}"' if ctrl.handler and not ctrl.disabled else ""
        opts = "".join(
            f'<option value="{_esc(opt.value)}"{" selected" if opt.selected else ""}>'
            f"{_esc(opt.label)}</option>"
            for opt in ctrl.options
        )
        control_el = f'<select id="{fid}"{evt}{dis}>{opts}</select>'
    else:
        attrs = [f'id="{fid}"', 'type="number"', f'value="{_esc(ctrl.value)}"']
        if ctrl.min_val is not None:
            attrs.append(f'min="{_esc(ctrl.min_val)}"')
        if ctrl.max_val is not None:
            attrs.append(f'max="{_esc(ctrl.max_val)}"')
        if ctrl.step is not None:
            attrs.append(f'step="{_esc(ctrl.step)}"')
        if ctrl.handler and not ctrl.disabled:
            attrs.append(f'oninput="{_esc(ctrl.handler)}"')
        control_el = f"<input {' '.join(attrs)}{dis} />"
    return f'<div class="field"><label for="{fid}">{_esc(ctrl.label)}</label>{control_el}</div>'


def _render_banner(banner: BannerView) -> str:
    id_attr = f' id="{_esc(banner.element_id)}"' if banner.element_id else ""
    return f'<div{id_attr} class="{_esc(banner.tone)}-banner">{_esc(banner.text)}</div>'


def _render_card(card: CardView) -> str:
    parts = [f"<h2>{_esc(card.title)}</h2>"]
    if card.subtitle is not None:
        parts.append(f'<p class="sub">{_esc(card.subtitle)}</p>')
    if card.controls:
        controls_html = "".join(_render_control(c) for c in card.controls)
        parts.append(f'<div class="controls-grid">{controls_html}</div>')
    if card.banner is not None:
        parts.append(_render_banner(card.banner))
    if card.table is not None:
        parts.append(_render_table(card.table))
    if card.code_block is not None:
        parts.append(f"<pre><code>{_esc(card.code_block)}</code></pre>")
    return f'<div class="card">{"".join(parts)}</div>'


def _render_rows(rows: tuple[CardRowView, ...]) -> str:
    blocks: list[str] = []
    for row in rows:
        if not row.cards:
            continue
        rendered_cards = "".join(_render_card(c) for c in row.cards)
        _, *extra_cards = row.cards
        if extra_cards:
            blocks.append(f'<div class="two-col">{rendered_cards}</div>')
        else:
            blocks.append(rendered_cards)
    return "".join(blocks)


def _render_tabs(tabs: tuple[TabView, ...]) -> str:
    buttons = "".join(
        f'<button class="tab-btn{" active" if tab.active else ""}" '
        f"onclick=\"switchTab('{_esc(tab.panel_id)}', this)\">"
        f"{_esc(tab.button_label)}</button>"
        for tab in tabs
    )
    panels = "".join(
        f'<section id="{_esc(tab.panel_id)}" class="tab-panel{" active" if tab.active else ""}">'
        f"{_render_kpi_grid(tab.kpis)}{_render_rows(tab.rows)}</section>"
        for tab in tabs
    )
    return f'<nav class="tabs" role="tablist">{buttons}</nav>{panels}'


def render_page_view(view: PageView) -> str:
    """Turn any typed `PageView` into self-contained HTML, escaping every slot in `_esc`."""
    header_parts = [_render_badge(b) for b in view.badges]
    if view.docs_link is not None:
        header_parts.append(
            f'<a href="{_esc(view.docs_link.href)}" class="docs-link">'
            f"{_esc(view.docs_link.label)}</a>"
        )

    main_attrs_list = [f'data-live-api="{"true" if view.live_api else "false"}"']
    for attr_name, attr_val in view.api_context:
        main_attrs_list.append(f'{_esc(attr_name)}="{_esc(attr_val)}"')
    main_attrs = " " + " ".join(main_attrs_list)

    if view.tabs:
        main_content = _render_tabs(view.tabs)
    else:
        main_content = _render_kpi_grid(view.standalone_kpis) + _render_rows(view.standalone_rows)

    script_block = f"  <script>\n{_JS_SCRIPT}  </script>" if view.include_script else ""

    replacements = {
        "__PAGE_TITLE__": _esc(view.page_title),
        "__FAVICON_URI__": FAVICON_DATA_URI,
        "__STYLESHEET__": _CSS_STYLES,
        "__BRAND_HEADING__": _esc(view.brand_heading),
        "__BRAND_SUBTITLE__": _esc(view.brand_subtitle),
        "__HEADER_RIGHT__": " ".join(header_parts),
        "__MAIN_ATTRS__": main_attrs,
        "__MAIN_CONTENT__": main_content,
        "__SCRIPT_BLOCK__": script_block,
    }
    placeholder = re.compile("|".join(re.escape(key) for key in replacements))
    return placeholder.sub(lambda m: replacements[m.group()], _HTML_SHELL)


def render_recommendations_html(
    recs: RecommendationResponse,
    *,
    static_export: bool = True,
) -> str:
    """Render the sizing-only HTML scorecard for `recommend --html-out`."""
    return render_page_view(build_recommendations_page_view(recs, static_export=static_export))


def render_showcase_portal_html(
    report: ShowcaseReport,
    *,
    static_export: bool = False,
) -> str:
    """Render the self-contained GM3 Light Web Workbench from a typed `ShowcaseReport`."""
    return render_page_view(build_showcase_page_view(report, static_export=static_export))
