#!/usr/bin/env python3
"""ICP Rewriter: rebuild your ideal customer profile from the accounts that actually pay.

Reads an account export (domain + plan), enriches every domain with Crustdata, compares
paying accounts with free ones, writes the winning profile, and finds new companies that match it.
Data layer: Crustdata POST /batch/company/enrich and POST /company/search (https://docs.crustdata.com).

  load_accounts() -> your export, paying vs free
  profile()       -> one flat record per enriched company (size, stage, industry, country)
  compare()       -> conversion rate and lift for every value of every trait
  build_icp()     -> the values that convert clearly better, with a minimum sample size
  build_filters() -> the ICP as a company search
  run()           -> the whole pipeline
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from crustdata_api import Crustdata, load_env, normalize_domain
from notify import ask_claude, send_notion, send_slack

ENRICH_FIELDS = ["basic_info", "headcount.total", "funding.last_round_type",
                 "funding.total_investment_usd", "taxonomy.professional_network_industry", "locations.country"]
SEARCH_FIELDS = ["basic_info", "headcount.total", "funding.last_round_type",
                 "taxonomy.professional_network_industry", "locations.country"]

FREE_VALUES = {"", "free", "false", "f", "0", "no", "none", "null", "trial", "hobby", "starter_free"}
TRAITS = ["size", "stage", "industry", "country"]
# (label, min headcount, max headcount or None)
SIZE_BUCKETS = [("1-10", 1, 10), ("11-50", 11, 50), ("51-200", 51, 200), ("201-500", 201, 500),
                ("501-1000", 501, 1000), ("1000+", 1001, None)]
NO_ROUND = {"", "none", "unknown"}


def load_accounts(path: str, domain_col: str = "domain", plan_col: str = "plan") -> List[Dict[str, Any]]:
    """Read the export. A plan of free/false/0/trial/empty means not paying; anything else pays."""
    seen: Dict[str, Dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or domain_col not in reader.fieldnames or plan_col not in reader.fieldnames:
            raise SystemExit(f"{path} needs the columns '{domain_col}' and '{plan_col}' (see --domain-col, --plan-col)")
        for row in reader:
            domain = normalize_domain(row.get(domain_col))
            if not domain or "." not in domain:
                continue
            paying = (row.get(plan_col) or "").strip().lower() not in FREE_VALUES
            # a domain with several users: it pays if any of its rows pays
            if domain not in seen or (paying and not seen[domain]["paying"]):
                seen[domain] = {"domain": domain, "paying": paying}
    return list(seen.values())


def size_bucket(employees: Optional[float]) -> str:
    if not employees or employees < 1:
        return "unknown"
    for label, lo, hi in SIZE_BUCKETS:
        if employees >= lo and (hi is None or employees <= hi):
            return label
    return "unknown"


def stage_of(funding: Dict[str, Any]) -> str:
    rnd = (funding.get("last_round_type") or "").strip().lower()
    if rnd not in NO_ROUND:
        return rnd
    return "funded (round unknown)" if (funding.get("total_investment_usd") or 0) > 0 else "unfunded"


def profile(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Flatten one batch record `{original_identifier, internal_id, data}`."""
    data = record.get("data") or {}
    domain = normalize_domain(record.get("original_identifier") or (data.get("basic_info") or {}).get("primary_domain"))
    if not domain or not data:
        return None
    employees = (data.get("headcount") or {}).get("total")
    return {
        "domain": domain,
        "size": size_bucket(employees),
        "stage": stage_of(data.get("funding") or {}),
        "industry": (data.get("taxonomy") or {}).get("professional_network_industry") or "unknown",
        "country": (data.get("locations") or {}).get("country") or "unknown",
    }


def compare(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """For every trait value: accounts, paying, conversion rate, lift vs the overall rate."""
    total = len(rows)
    paying_total = sum(1 for r in rows if r["paying"])
    base = paying_total / total if total else 0.0
    out: Dict[str, List[Dict[str, Any]]] = {}
    for trait in TRAITS:
        seen: Counter = Counter()
        paid: Counter = Counter()
        for r in rows:
            seen[r[trait]] += 1
            paid[r[trait]] += 1 if r["paying"] else 0
        stats = []
        for value, n in seen.items():
            rate = paid[value] / n
            stats.append({"value": value, "accounts": n, "paying": paid[value], "rate": round(rate, 3),
                          "lift": round(rate / base, 2) if base else 0.0,
                          "share_of_paying": round(paid[value] / paying_total, 3) if paying_total else 0.0,
                          "share_of_free": round((n - paid[value]) / (total - paying_total), 3)
                          if total > paying_total else 0.0})
        out[trait] = sorted(stats, key=lambda s: (-s["lift"], -s["accounts"]))
    return out


def build_icp(stats: Dict[str, List[Dict[str, Any]]], min_support: int = 3, min_lift: float = 1.25,
              top: int = 3) -> Dict[str, List[str]]:
    """Keep values with enough accounts behind them that convert clearly better than average."""
    icp: Dict[str, List[str]] = {}
    for trait, rows in stats.items():
        keep = [s["value"] for s in rows
                if s["accounts"] >= min_support and s["lift"] >= min_lift and s["value"] != "unknown"]
        if keep:
            icp[trait] = keep[:top]
    return icp


def _size_condition(labels: Iterable[str]) -> Dict[str, Any]:
    ranges = {label: (lo, hi) for label, lo, hi in SIZE_BUCKETS}
    parts = []
    for label in labels:
        lo, hi = ranges[label]
        conds = [{"field": "headcount.total", "type": "=>", "value": lo}]
        if hi is not None:
            conds.append({"field": "headcount.total", "type": "=<", "value": hi})
        parts.append({"op": "and", "conditions": conds})
    return parts[0] if len(parts) == 1 else {"op": "or", "conditions": parts}


def _match(field: str, values: List[str]) -> Dict[str, Any]:
    if len(values) == 1:
        return {"field": field, "type": "=", "value": values[0]}
    return {"field": field, "type": "in", "value": values}


def build_filters(icp: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    """The ICP as a /company/search filter. Returns None when the ICP is empty."""
    conds: List[Dict[str, Any]] = []
    if icp.get("size"):
        conds.append(_size_condition(icp["size"]))
    rounds = [s for s in icp.get("stage", []) if s not in ("unfunded", "funded (round unknown)")]
    if rounds and len(rounds) == len(icp.get("stage", [])):  # filter only when every winner is a real round
        conds.append(_match("funding.last_round_type", rounds))
    if icp.get("industry"):
        conds.append(_match("taxonomy.professional_network_industry", icp["industry"]))
    if icp.get("country"):
        conds.append(_match("locations.country", icp["country"]))
    return {"op": "and", "conditions": conds} if conds else None


def target_row(company: Dict[str, Any]) -> Dict[str, Any]:
    info = company.get("basic_info") or {}
    return {
        "name": info.get("name") or "",
        "domain": normalize_domain(info.get("primary_domain")),
        "employees": (company.get("headcount") or {}).get("total"),
        "stage": stage_of(company.get("funding") or {}),
        "industry": (company.get("taxonomy") or {}).get("professional_network_industry") or "",
        "country": (company.get("locations") or {}).get("country") or "",
    }


def find_targets(client: Crustdata, icp: Dict[str, List[str]], exclude: Iterable[str],
                 max_targets: int) -> Tuple[List[Dict[str, Any]], int]:
    """Companies matching the ICP that are not already in the export. Returns (rows, total matches)."""
    filters = build_filters(icp)
    if not filters or max_targets <= 0:
        return [], 0
    known = set(exclude)
    total = client.company_count(filters)
    out: List[Dict[str, Any]] = []
    # over-fetch a little: some matches are accounts you already have
    for company in client.company_search(filters, SEARCH_FIELDS, max_results=max_targets + len(known)):
        row = target_row(company)
        if row["domain"] and row["domain"] not in known:
            known.add(row["domain"])
            out.append(row)
            if len(out) >= max_targets:
                break
    return out, total


def run(client: Crustdata, accounts: List[Dict[str, Any]], min_support: int = 3, min_lift: float = 1.25,
        max_targets: int = 100) -> Dict[str, Any]:
    paying = {a["domain"] for a in accounts if a["paying"]}
    if not paying or len(paying) == len(accounts):
        raise SystemExit("The export needs both paying and free accounts to compare them.")
    records = client.batch_company_enrich([a["domain"] for a in accounts], ENRICH_FIELDS)
    profiles = {p["domain"]: p for p in (profile(r) for r in records) if p}
    rows = [dict(profiles[a["domain"]], paying=a["paying"]) for a in accounts if a["domain"] in profiles]
    if not rows or not any(r["paying"] for r in rows) or all(r["paying"] for r in rows):
        raise SystemExit("Too few enriched accounts on both sides to compare.")
    stats = compare(rows)
    icp = build_icp(stats, min_support=min_support, min_lift=min_lift)
    targets, matches = find_targets(client, icp, [a["domain"] for a in accounts], max_targets)
    return {"accounts": len(accounts), "enriched": len(rows), "stats": stats, "icp": icp,
            "targets": targets, "matches_total": matches}


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    stats = result["stats"]
    # every trait splits the same enriched accounts, so any trait gives the paying / free counts
    first = next(iter(stats.values()), [])
    paying = sum(s["paying"] for s in first)
    enriched = sum(s["accounts"] for s in first)
    return {"accounts_in_export": result["accounts"], "enriched": result["enriched"],
            "paying": paying, "free": enriched - paying,
            "icp": result["icp"], "targets_returned": len(result["targets"]),
            "icp_matches_in_index": result["matches_total"]}


def icp_markdown(result: Dict[str, Any], narrative: Optional[str] = None) -> str:
    s = summary(result)
    lines = ["# Ideal customer profile (from paying accounts)", "",
             f"Based on {s['paying']} paying and {s['free']} free accounts, enriched with Crustdata.", ""]
    if narrative:
        lines += [narrative.strip(), ""]
    lines += ["## Winning profile", ""]
    if result["icp"]:
        lines += [f"- **{trait}**: {', '.join(vals)}" for trait, vals in result["icp"].items()]
    else:
        lines.append("- No trait converts clearly better than average at this sample size.")
    lines += ["", "## What converts", "", "| Trait | Value | Accounts | Paying | Conversion | Lift |", "|---|---|---|---|---|---|"]
    for trait, rows in result["stats"].items():
        for st in rows[:5]:
            lines.append(f"| {trait} | {st['value']} | {st['accounts']} | {st['paying']} | "
                         f"{round(st['rate'] * 100)}% | {st['lift']}x |")
    return "\n".join(lines) + "\n"


def claude_prompt(result: Dict[str, Any]) -> str:
    return ("You are a PLG growth analyst. Below are conversion stats from paying vs free accounts "
            "(lift = conversion rate for that value divided by the overall rate) and the rule-based ICP. "
            "Write a short ICP for the team: who buys, who does not, and one risk caveat about sample size. "
            "Use only these numbers, no invented facts, under 150 words.\n\n"
            + json.dumps({"icp": result["icp"], "stats": {t: r[:6] for t, r in result["stats"].items()}}))


def slack_text(result: Dict[str, Any]) -> str:
    s = summary(result)
    icp = "; ".join(f"{t}: {', '.join(v)}" for t, v in result["icp"].items()) or "no clear winner yet"
    return (f":dart: ICP refreshed from {s['paying']} paying and {s['free']} free accounts\n"
            f"• Winning profile: {icp}\n• {s['targets_returned']} new target companies "
            f"({s['icp_matches_in_index']} match in total)")


def _load_state(state_path: str) -> set:
    if not os.path.exists(state_path):
        return set()
    with open(state_path, encoding="utf-8") as fh:
        return set(json.load(fh))


def only_new(rows: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = _load_state(state_path)
    return [r for r in rows if r["domain"] not in seen]


def save_state(rows: List[Dict[str, Any]], state_path: str) -> None:
    seen = _load_state(state_path)
    seen.update(r["domain"] for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh, indent=1)


def write_outputs(result: Dict[str, Any], markdown: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "icp.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary(result), "stats": result["stats"]}, fh, indent=1)
    cols = ["name", "domain", "employees", "stage", "industry", "country"]
    with open(os.path.join(out_dir, "targets.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(result["targets"])


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Rebuild your ICP from paying accounts (Crustdata data layer)")
    p.add_argument("--accounts", default=os.environ.get("ACCOUNTS_CSV", "accounts.csv"))
    p.add_argument("--domain-col", default="domain")
    p.add_argument("--plan-col", default="plan")
    p.add_argument("--min-support", type=int, default=int(os.environ.get("MIN_SUPPORT", "3")))
    p.add_argument("--min-lift", type=float, default=float(os.environ.get("MIN_LIFT", "1.25")))
    p.add_argument("--max-targets", type=int, default=100)
    p.add_argument("--claude", action="store_true", help="have Claude write the ICP summary")
    p.add_argument("--notion", action="store_true", help="publish the ICP and targets to Notion")
    p.add_argument("--dry-run", action="store_true", help="never post to Slack or Notion")
    p.add_argument("--all", action="store_true", help="include targets you were already sent")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    result = run(Crustdata(), load_accounts(args.accounts, args.domain_col, args.plan_col),
                 args.min_support, args.min_lift, args.max_targets)
    if not args.all:
        result["targets"] = only_new(result["targets"], args.state)
    narrative = ask_claude(claude_prompt(result)) if args.claude else None
    markdown = icp_markdown(result, narrative)
    write_outputs(result, markdown, args.out)
    print(json.dumps(summary(result), indent=1))
    print(send_slack(slack_text(result), dry_run=args.dry_run))
    if args.notion:
        print(send_notion("ICP (from paying accounts)", markdown, result["targets"], dry_run=args.dry_run))
    if not args.dry_run:
        save_state(result["targets"], args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
