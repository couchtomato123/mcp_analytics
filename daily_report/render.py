"""Renders the report model as a mobile-friendly HTML email (spec §8).

Email clients ignore <style> blocks and most layout CSS, so everything is
inline styles and tables, in a single column that reads well on a phone.
"""
from html import escape

from .kpis import KPI_DEFS, money

UNAVAILABLE = "Data unavailable"
INK, MUTED, LINE, ACCENT, UP, DOWN, WARN_BG = "#1a1a1a", "#666666", "#e3e3e3", "#1f3a5f", "#1b7f3b", "#b42318", "#fff4e5"
CPL_CAVEAT = (
    "True CPL = Google Ads spend ÷ Salesforce leads whose LeadSource is {sources}. "
    "Google Ads' own “conversions” are platform-reported micro-conversions (historically ~5x higher than "
    "real leads) and are never used to calculate CPL."
)


def subject(report: dict) -> str:
    return f"Barrister Daily Marketing + Sales Brief | {report['period'].label()}"


def _num(value, currency=False):
    if value is None:
        return "–"
    if currency:
        return money(value)
    # Averages of small daily counts (e.g. 2.4 tours) need the decimal.
    return f"{value:,.1f}" if not float(value).is_integer() and abs(value) < 10 else f"{value:,.0f}"


def _change(change):
    if change is None:
        return f'<span style="color:{MUTED}">–</span>'
    color = UP if change > 0 else DOWN if change < 0 else MUTED
    arrow = "▲" if change > 0 else "▼" if change < 0 else ""
    return f'<span style="color:{color};white-space:nowrap">{arrow}&nbsp;{abs(change):.0%}</span>'


def _h2(text):
    return (f'<h2 style="font-size:16px;color:{ACCENT};margin:28px 0 8px;padding-bottom:4px;'
            f'border-bottom:2px solid {ACCENT}">{escape(text)}</h2>')


def _note(text):
    return f'<p style="font-size:12px;color:{MUTED};margin:6px 0 0">{text}</p>'


def _unavailable(source_label):
    return f'<p style="color:{DOWN};margin:4px 0">{UNAVAILABLE} ({escape(source_label)}).</p>'


def _table(headers, rows, align=None):
    align = align or ["left"] + ["right"] * (len(headers) - 1)
    head = "".join(
        f'<th style="text-align:{a};padding:6px 4px;font-size:12px;color:{MUTED};font-weight:600;'
        f'border-bottom:1px solid {LINE}">{escape(h)}</th>'
        for h, a in zip(headers, align)
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td style="text-align:{a};padding:6px 4px;border-bottom:1px solid {LINE};font-size:14px">{c}</td>'
            for c, a in zip(row, align)
        ) + "</tr>"
        for row in rows
    )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse"><tr>{head}</tr>{body}</table>')


def _tile(label, value, sub=""):
    sub_html = f'<div style="font-size:12px;color:{MUTED};margin-top:2px">{sub}</div>' if sub else ""
    return (
        '<td style="padding:4px;width:50%;vertical-align:top">'
        f'<div style="border:1px solid {LINE};border-radius:8px;padding:10px 12px">'
        f'<div style="font-size:12px;color:{MUTED}">{escape(label)}</div>'
        f'<div style="font-size:24px;font-weight:700;color:{INK};margin-top:2px">{value}</div>{sub_html}</div></td>'
    )


def _glance(r):
    t, cmp = r["totals"], {row["key"]: row for row in r["comparison"]}
    if not r["available"]["salesforce"]:
        return _h2("At a Glance") + _unavailable("Salesforce")

    def sub(key):
        return f"vs 7-day avg {_change(cmp[key]['change'])}"

    mix = ", ".join(f"{n} {escape(kind.lower())}" for kind, n in r["deal_mix"]) or "none"
    tiles = [
        _tile("New leads", _num(t["new_leads"]), sub("new_leads")),
        _tile("Qualified leads", _num(t["qualified_leads"]), sub("qualified_leads")),
        _tile("Tours", _num(t["tours"]), sub("tours")),
        _tile("Signed deals", _num(t["signed_deals"]),
              f"<b>{_num(t['new_business_deals'])} new business</b> · {mix}"),
        _tile("New monthly revenue", _num(t["new_mrr"], True),
              f"{_num(r['new_business_mrr'], True)} from new business · "
              f"{_num(r['one_time_revenue'], True)} one-time"),
    ]
    rows = "".join(f"<tr>{''.join(tiles[i:i + 2])}</tr>" for i in range(0, len(tiles), 2))
    title = "Yesterday at a Glance" if not r["period"].is_multi_day else "Weekend at a Glance"
    return (_h2(title) + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
            + _note("Signed deals counts every Closed Won opportunity, including renewals and rent changes. "
                    "New business means Contract Change Type = New Client. Qualified leads are leads created in "
                    "the period whose status is now Qualified, so the newest day reads low until reps work them."))


def _comparison(r):
    p = r["period"]
    n = len(p.days)
    expected_label = "7-day avg" if n == 1 else f"7-day avg ×{n}"
    sources = {key: source for key, _, source, _ in KPI_DEFS}
    rows = []
    for row in r["comparison"]:
        if not r["available"][sources[row["key"]]]:
            rows.append([escape(row["label"]), UNAVAILABLE, "", ""])
            continue
        rows.append([escape(row["label"]), _num(row["actual"], row["currency"]),
                     _num(row["expected"], row["currency"]), _change(row["change"])])
    html = _h2(f"{p.short_label()} vs 7-Day Average") + _table(["Metric", p.short_label(), expected_label, "Δ"], rows)
    if r["per_day"]:
        day_rows = [
            [d["date"].strftime("%a %-d"), _num(d["new_leads"]), _num(d["qualified_leads"]),
             _num(d["tours"]), _num(d["signed_deals"]), _num(d["new_mrr"], True)]
            for d in r["per_day"]
        ]
        html += ('<p style="font-size:13px;margin:14px 0 4px;font-weight:600">By day</p>'
                 + _table(["Day", "Leads", "Qual.", "Tours", "Deals", "New MRR"], day_rows))
    return html


def _lead_sources(r):
    html = _h2("Lead Sources")
    if r["lead_sources"] is None:
        html += _unavailable("Salesforce")
    elif not r["lead_sources"]:
        html += "<p>No new leads.</p>"
    else:
        top = r["lead_sources"][:10]
        rows = [[escape(s["source"]), str(s["leads"]), str(s["qualified"])] for s in top]
        rest = r["lead_sources"][10:]
        if rest:
            rows.append([f"{len(rest)} other sources", str(sum(s["leads"] for s in rest)),
                         str(sum(s["qualified"] for s in rest))])
        html += _table(["Salesforce LeadSource", "Leads", "Qualified"], rows)

    html += '<p style="font-size:13px;margin:14px 0 4px;font-weight:600">Website traffic (GA4)</p>'
    web = r["web_traffic"]
    if web is None:
        return html + _unavailable("Google Analytics")
    rows = [[escape(c["sessionDefaultChannelGroup"]), _num(c["sessions"]), _num(c["keyEvents"])] for c in web["channels"]]
    html += _table(["Channel", "Sessions", "Key events"], rows) if rows else "<p>No sessions recorded.</p>"
    if web["landing_pages"]:
        rows = [[escape(p["landingPage"] or "(not set)"), _num(p["sessions"]), _num(p["keyEvents"])] for p in web["landing_pages"]]
        html += '<div style="height:10px"></div>' + _table(["Top landing pages", "Sessions", "Key events"], rows)
    return html + _note("GA4 key events are platform-reported and are not leads. Lead counts above come from Salesforce.")


def _product_demand(r):
    html = _h2("Product Demand")
    if r["product_demand"] is None:
        return html + _unavailable("Salesforce")
    if not r["product_demand"]:
        return html + "<p>No leads or deals.</p>"
    rows = [[escape(p["product"]), str(p["leads"]), str(p["deals"]), _num(p["new_mrr"], True)] for p in r["product_demand"]]
    return html + _table(["Product", "Leads", "Deals", "New MRR"], rows) + _note(
        "Leads by Lead Type; deals by Opportunity Client Type.")


def _properties(r):
    html = _h2("Property Performance")
    if r["properties"] is None:
        return html + _unavailable("Salesforce")
    if not r["properties"]:
        return html + "<p>No property activity.</p>"
    rows = [[escape(p["property"]), str(p["leads"]), str(p["qualified"]), str(p["deals"]), _num(p["new_mrr"], True)]
            for p in r["properties"]]
    return html + _table(["Property", "Leads", "Qual.", "Deals", "New MRR"], rows)


def _pipeline(r):
    html = _h2("Sales Pipeline")
    pipe = r["pipeline"]
    if pipe is None:
        return html + _unavailable("Salesforce")
    rows = [[escape(s["stage"]), str(s["count"]), _num(s["recurring_revenue"], True)] for s in pipe["stages"]]
    rows.append(["<b>Total open</b>", f"<b>{pipe['open_count']}</b>", f"<b>{_num(pipe['open_mrr'], True)}</b>"])
    html += _table(["Open stage", "Opps", "Recurring revenue"], rows)
    deals = r["deals"] or []
    html += _note(
        f"This period: {pipe['created']} opportunities created, {len(deals)} closed won, "
        f"{pipe['closed_lost']} closed lost. {pipe['overdue_late_stage']} Negotiation/LOS opportunities are past their close date."
    )
    if deals:
        rows = [[escape(d["name"] or ""), escape(d["lease_change_type"] or "(not set)"),
                 _num(d["recurring_revenue"], True), _num(d["one_time_revenue"], True)] for d in deals[:10]]
        html += ('<p style="font-size:13px;margin:14px 0 4px;font-weight:600">Closed won</p>'
                 + _table(["Opportunity", "Type", "MRR", "One-time"], rows, ["left", "left", "right", "right"]))
    return html


def _paid(r):
    html = _h2("Paid Marketing: Google Ads")
    paid = r["paid"]
    ads = paid["ads"]
    caveat = _note(CPL_CAVEAT.format(sources=" / ".join(f"'{escape(s)}'" for s in paid["lead_sources"])))
    if ads is None:
        return html + _unavailable("Google Ads") + caveat
    sf_leads = UNAVAILABLE if paid["sf_leads"] is None else _num(paid["sf_leads"])
    if paid["true_cpl"] is not None:
        cpl = _num(paid["true_cpl"], True)
    elif paid["sf_leads"] == 0:
        cpl = "n/a (0 leads)"
    else:
        cpl = UNAVAILABLE if paid["sf_leads"] is None else "–"
    rows = [
        ["Spend", _num(ads["spend"], True)],
        ["Clicks", _num(ads["clicks"])],
        ["CPC", _num(paid["cpc"], True)],
        ["Salesforce leads (Google Ads)", sf_leads],
        ["<b>True cost per lead</b>", f"<b>{cpl}</b>"],
        ["7-day true CPL", _num(paid["baseline_cpl"], True)],
        ["Conversions (platform-reported)", "–" if ads["conversions"] is None else f'{ads["conversions"]:,.1f}'],
    ]
    html += _table(["Metric", "Value"], rows)
    if paid["discrepancy"]:
        html += (f'<p style="background:{WARN_BG};padding:8px 10px;border-radius:6px;font-size:13px;margin:8px 0 0">'
                 f"⚠ Platform-reported conversions ({ads['conversions']:,.1f}) and Salesforce Google Ads leads "
                 f"({paid['sf_leads']}) differ by more than 2x. Neither number has been substituted for the other.</p>")
    if paid["campaigns"]:
        rows = [[escape(c["name"]), _num(c["spend"], True), _num(c["clicks"]), f'{c["conversions"]:,.1f}']
                for c in paid["campaigns"]]
        html += ('<p style="font-size:13px;margin:14px 0 4px;font-weight:600">Top campaigns by spend</p>'
                 + _table(["Campaign", "Spend", "Clicks", "Platform conv."], rows))
    return html + caveat


def _bullets(title, items):
    lis = "".join(f'<li style="margin:0 0 6px">{escape(i)}</li>' for i in items)
    return _h2(title) + f'<ul style="padding-left:20px;margin:6px 0">{lis}</ul>'


def render_html(report: dict) -> str:
    p = report["period"]
    when = "yesterday" if not p.is_multi_day else "over the weekend"
    sections = [
        _glance(report),
        _comparison(report),
        _lead_sources(report),
        _product_demand(report),
        _properties(report),
        _pipeline(report),
        _paid(report),
        _bullets(f"What Happened {'Yesterday' if not p.is_multi_day else 'Over the Weekend'}", report["happened"]),
        _bullets("Today's Priorities", report["priorities"]),
    ]
    errors = "".join(f"<li>{escape(k)}: {escape(v)}</li>" for k, v in report["errors"].items())
    footer = _note(
        f"Covers {escape(p.label())} (Pacific time) {when}, compared with the 7 days before. "
        "Sources: Salesforce, Google Analytics 4, Google Ads."
        + (f"<br>Source errors:<ul>{errors}</ul>" if errors else "")
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(subject(report))}</title></head>
<body style="margin:0;padding:0;background:#f4f5f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7"><tr><td align="center" style="padding:12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:10px">
<tr><td style="padding:20px 18px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{INK};font-size:14px;line-height:1.45">
<div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">Barrister Executive Suites</div>
<h1 style="font-size:20px;margin:4px 0 0">Daily Marketing + Sales Brief</h1>
<div style="color:{MUTED};font-size:14px">{escape(p.label())}</div>
{''.join(sections)}
<div style="margin-top:28px;border-top:1px solid {LINE};padding-top:8px">{footer}</div>
</td></tr></table></td></tr></table></body></html>"""


def render_text(report: dict) -> str:
    """Plain-text alternative part for clients that block HTML."""
    lines = [subject(report), ""]
    for row in report["comparison"]:
        value = UNAVAILABLE if row["actual"] is None else _num(row["actual"], row["currency"])
        lines.append(f"{row['label']}: {value}")
    lines += ["", "What happened:"] + [f"- {b}" for b in report["happened"]]
    lines += ["", "Today's priorities:"] + [f"- {b}" for b in report["priorities"]]
    lines += ["", "Open the HTML version for the full tables."]
    return "\n".join(lines)
