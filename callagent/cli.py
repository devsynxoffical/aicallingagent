"""Command-line interface.

    callagent onboard script.md --name "Acme Q4"   # learn the pitch, answer its questions
    callagent rehearse "Acme Q4" --apply           # self-train against simulated prospects
    callagent leads import "Acme Q4" leads.xlsx    # load the sheet
    callagent serve                                # start webhooks + media server
    callagent campaign start "Acme Q4"             # dial
    callagent campaign status "Acme Q4"
    callagent campaign export "Acme Q4" results.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .config import get_settings, resolve_api_token
from .db import Campaign, get_campaign_by_name, session_scope
from .playbook.schema import Playbook, QAPair

app = typer.Typer(help="Outbound AI calling agent that learns your pitch, dials your sheet and closes.", no_args_is_help=True)
leads_app = typer.Typer(help="Lead sheets.")
campaign_app = typer.Typer(help="Run and monitor campaigns (needs `callagent serve` running).")
playbook_app = typer.Typer(help="Inspect and edit playbooks.")
train_app = typer.Typer(help="Teach the agent from documents and websites.")
app.add_typer(leads_app, name="leads")
app.add_typer(campaign_app, name="campaign")
app.add_typer(playbook_app, name="playbook")
app.add_typer(train_app, name="train")
db_app = typer.Typer(help="Database schema: status, upgrade, integrity check.")
app.add_typer(db_app, name="db")
users_app = typer.Typer(help="People who sign in to the console, and their roles.")
app.add_typer(users_app, name="users")
orgs_app = typer.Typer(help="Customer organizations on this deployment.")
app.add_typer(orgs_app, name="orgs")

console = Console()

CALL_COLUMNS = ("call_run_id", "lead", "phone", "direction", "language", "status", "mode", "disposition", "duration_seconds", "summary")


def _playbook_path(name: str) -> Path:
    settings = get_settings()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower())
    return settings.data_dir / "playbooks" / f"{safe}.json"


def _save_campaign(name: str, playbook: Playbook, script_text: str, source: str = "onboard") -> Campaign:
    path = _playbook_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(playbook.model_dump_json(indent=2))
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            c = Campaign(name=name, status="draft")
            s.add(c)
        c.script_text = script_text or c.script_text
        s.flush()
        from .training import versions

        # Recorded as a new version and made active (the owner is changing it directly).
        # Running/paused/done campaigns keep their state; drafts become ready once nothing blocks.
        versions.record_active(s, c, playbook.model_dump(), source=source)
        s.flush()
        s.refresh(c)
        return c


def _load_playbook(name: str) -> tuple[Playbook, str]:
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            raise typer.BadParameter(f"campaign '{name}' not found. Run `callagent onboard` first.")
        return Playbook.model_validate(c.playbook_json), c.script_text


def _print_playbook_summary(pb: Playbook) -> None:
    console.print(Panel.fit(
        f"[bold]{pb.company_name}[/bold]\n{pb.offer_summary}\n\n"
        f"Goal: [cyan]{pb.call_goal}[/cyan] - {pb.call_goal_details}\n"
        f"Agent: {pb.persona.agent_name}, {pb.persona.role_title} ({pb.persona.tone})\n"
        f"Languages: {', '.join(pb.languages)}"
        f"{' (follows the prospect)' if pb.follows_prospect_language else ''}"
        f"{'   native phrasing: ' + ', '.join(l.language for l in pb.localized) if pb.localized else ''}\n"
        f"Stages: {', '.join(s.name for s in pb.stages)}\n"
        f"Objections covered: {len(pb.objections)}   Key terms: {len(pb.keyterms)}\n"
        f"Capabilities: {', '.join(c.key + ('*' if c.required else '') for c in pb.required_capabilities) or '-'}\n"
        f"Readiness: [bold]{pb.readiness_score}/100[/bold] - {pb.readiness_notes}",
        title="Playbook",
    ))


@app.command()
def onboard(
    script: Path = typer.Argument(..., exists=True, readable=True, help="The business's script / pitch / notes (text or markdown)."),
    name: str = typer.Option(..., "--name", "-n", help="Campaign name."),
    non_interactive: bool = typer.Option(False, help="Do not ask questions; accept suggested defaults."),
    max_rounds: int = typer.Option(3, help="Max Q&A rounds."),
):
    """Learn the pitch from a script, ask what's missing, and save the playbook."""
    from .playbook.analyzer import PlaybookAnalyzer

    text = script.read_text(encoding="utf-8")
    analyzer = PlaybookAnalyzer()
    with console.status("Reading the script and building the playbook..."):
        pb = analyzer.analyze(text)
    _print_playbook_summary(pb)

    answers: list[QAPair] = []
    rounds = 0
    while pb.open_questions and rounds < max_rounds:
        rounds += 1
        console.print(f"\n[bold]The agent has {len(pb.open_questions)} question(s) about your pitch[/bold] "
                      f"({len(pb.blocking_questions)} blocking):")
        new_answers: list[QAPair] = []
        for i, q in enumerate(pb.open_questions, 1):
            tag = "[red]blocking[/red]" if q.blocking else "[yellow]optional[/yellow]"
            console.print(f"\n{i}. {q.question}  {tag}\n   [dim]{q.why_it_matters}[/dim]")
            if non_interactive:
                if q.suggested_default:
                    new_answers.append(QAPair(question=q.question, answer=f"(default accepted) {q.suggested_default}"))
                continue
            default = q.suggested_default or None
            ans = Prompt.ask("   Your answer", default=default) if default else Prompt.ask("   Your answer (Enter to skip)", default="")
            if ans.strip():
                new_answers.append(QAPair(question=q.question, answer=ans.strip()))
        if not new_answers:
            break
        answers.extend(new_answers)
        with console.status("Folding your answers into the playbook..."):
            pb = analyzer.analyze(text, answers=answers, previous=pb)
        _print_playbook_summary(pb)

    c = _save_campaign(name, pb, text)
    console.print(f"\nSaved playbook to [green]{_playbook_path(name)}[/green] and campaign [bold]{c.name}[/bold] (status: {c.status}).")
    if pb.blocking_questions:
        console.print("[yellow]There are still blocking questions. Re-run onboard, or start with --force to dial anyway.[/yellow]")
    else:
        console.print("Next: [cyan]callagent rehearse[/cyan] to let the agent practice, then [cyan]callagent leads import[/cyan].")


@app.command()
def rehearse(
    name: str = typer.Argument(..., help="Campaign name."),
    persona: list[str] = typer.Option(None, "--persona", "-p", help="Prospect persona(s) to rehearse against."),
    turns: int = typer.Option(12, help="Max turns per rehearsal."),
    apply: bool = typer.Option(False, help="Apply the coach's playbook edits automatically."),
):
    """Let the agent practice the pitch against simulated prospects and get coached."""
    from .rehearsal import apply_coach_edits, run_rehearsal
    from .llm import make_client

    pb, script_text = _load_playbook(name)
    settings = get_settings()
    client = make_client(settings)

    def show(t):
        console.rule(f"[dim]{t.persona}[/dim]")
        for turn in t.turns:
            who = f"[cyan]{pb.persona.agent_name}[/cyan]" if turn["role"] == "agent" else "[magenta]Prospect[/magenta]"
            console.print(f"{who}: {turn['text']}")
        if t.tool_calls:
            console.print("[dim]tools: " + ", ".join(c["tool"] for c in t.tool_calls) + "[/dim]")

    with console.status("Rehearsing..."):
        transcripts, report = run_rehearsal(pb, persona or None, turns, settings, client, on_transcript=show)

    console.rule("Coach report")
    console.print(f"Score: [bold]{report.overall_score}/100[/bold]   Ready for real calls: {'[green]yes[/green]' if report.ready_for_real_calls else '[red]no[/red]'}")
    console.print(report.readiness_notes)
    for title, items in (("Strengths", report.strengths), ("Weaknesses", report.weaknesses),
                         ("Sounded robotic", report.sounded_robotic_examples), ("Tool discipline", report.tool_discipline_issues),
                         ("Suggested playbook edits", report.playbook_edits)):
        if items:
            console.print(f"\n[bold]{title}[/bold]")
            for it in items:
                console.print(f" - {it}")

    settings.data_dir.joinpath("rehearsals").mkdir(parents=True, exist_ok=True)
    out = settings.data_dir / "rehearsals" / f"{_playbook_path(name).stem}-{len(list(settings.data_dir.joinpath('rehearsals').glob('*.json'))) + 1}.json"
    out.write_text(json.dumps({"transcripts": [t.__dict__ for t in transcripts], "report": report.model_dump()}, indent=2, default=str))
    console.print(f"\nSaved to {out}")

    if report.playbook_edits and (apply or Confirm.ask("Apply these edits to the playbook?", default=False)):
        with console.status("Applying edits..."):
            pb = apply_coach_edits(client, pb, report, settings)
        _save_campaign(name, pb, script_text, source="rehearsal")
        console.print("[green]Playbook updated.[/green] Run rehearse again to verify.")


@app.command()
def chat(
    name: str = typer.Argument(..., help="Campaign name."),
    model: str = typer.Option(None, help="Override CALL_MODEL for this session (e.g. claude-sonnet-5)."),
    thinking: str = typer.Option(None, help="Override CALL_THINKING: disabled | adaptive."),
    fast: bool = typer.Option(False, help="Use fast mode for this session."),
):
    """Talk to the agent in the terminal with the live-call model settings. Shows time-to-first-word per turn."""
    from .chat import ChatSession

    pb, _ = _load_playbook(name)
    settings = get_settings().model_copy(update={k: v for k, v in {"call_model": model, "call_thinking": thinking, "call_fast_mode": fast or None}.items() if v})
    session = ChatSession(pb, settings)
    console.print(Panel.fit(
        f"model [bold]{settings.call_model}[/bold] · thinking {settings.call_thinking} · effort {settings.call_effort}"
        f"{' · fast mode' if settings.call_fast_mode else ''}\nYou are the prospect. Type what you'd say on the phone. Ctrl-C to stop.",
        title=f"Chat with {pb.persona.agent_name}"))
    user_text = "Hello?"
    console.print(f"[magenta]You[/magenta]: {user_text}")
    try:
        while not session.ended:
            console.print(f"[cyan]{pb.persona.agent_name}[/cyan]: ", end="")
            reply, st = session.say(user_text, on_text=lambda t: console.print(t, end="", highlight=False))
            console.print()
            tools = f"  tools: {', '.join(st.tools)}" if st.tools else ""
            console.print(f"[dim]  first word {st.ttft_secs:.2f}s · full reply {st.total_secs:.2f}s · {st.output_tokens} tokens{tools}[/dim]")
            if session.ended:
                break
            user_text = Prompt.ask("[magenta]You[/magenta]")
    except KeyboardInterrupt:
        pass
    if session.stats:
        ttfts = sorted(s.ttft_secs for s in session.stats)
        console.print(f"\n[bold]Model latency this session[/bold]: first word p50 {ttfts[len(ttfts)//2]:.2f}s, max {ttfts[-1]:.2f}s over {len(ttfts)} turns. "
                      "On the phone add roughly 0.3s for speech recognition and 0.2s for the voice.")


@playbook_app.command("show")
def playbook_show(name: str, full: bool = typer.Option(False, help="Print the whole JSON.")):
    pb, _ = _load_playbook(name)
    if full:
        console.print_json(pb.model_dump_json())
    else:
        _print_playbook_summary(pb)
        if pb.open_questions:
            console.print("\n[bold]Open questions[/bold]")
            for q in pb.open_questions:
                console.print(f" - {'[red]*[/red] ' if q.blocking else ''}{q.question}")


@playbook_app.command("prompt")
def playbook_prompt(name: str, voicemail: bool = False):
    """Print the live-call system prompt the agent will run with (for review)."""
    from .playbook.prompt_builder import build_system_prompt

    pb, _ = _load_playbook(name)
    lead = {"first_name": "Sam", "last_name": "Taylor", "company": "Taylor & Co", "timezone": get_settings().default_timezone}
    console.print(build_system_prompt(pb, lead, mode="voicemail" if voicemail else "live", callback_number=get_settings().twilio_from_number or ""))


@playbook_app.command("languages")
def playbook_languages(
    name: str,
    languages: list[str] = typer.Argument(..., help="Languages the agent may speak, opening language first, e.g. en ar fr"),
    localize: bool = typer.Option(True, help="Write native phrasing for each added language now."),
):
    """Set the languages a campaign speaks. With more than one, the agent follows the prospect."""
    from .languages import LANGUAGE_NAMES, base_language
    from .providers import SpeechPlan, speech_plan_problems

    pb, script = _load_playbook(name)
    codes = list(dict.fromkeys(base_language(l) for l in languages if base_language(l)))
    unknown = [c for c in codes if c not in LANGUAGE_NAMES]
    if unknown:
        raise typer.BadParameter(f"unsupported language(s): {', '.join(unknown)}. Supported: {', '.join(LANGUAGE_NAMES)}")
    if base_language(pb.persona.language) != codes[0]:
        pb.persona.language = codes[0]
    pb.additional_languages = codes[1:]
    pb.localized = [l for l in pb.localized if base_language(l.language) in codes[1:]]
    if localize:
        from .playbook.analyzer import PlaybookAnalyzer

        analyzer = PlaybookAnalyzer()
        for code in codes[1:]:
            if pb.localized_for(code) is None:
                with console.status(f"Writing native {LANGUAGE_NAMES[code]} phrasing..."):
                    pb.localized.append(analyzer.localize(pb, code))
    _save_campaign(name, pb, script, source="languages")
    _print_playbook_summary(pb)
    for problem in speech_plan_problems(get_settings(), SpeechPlan.from_playbook(pb)):
        console.print(f"[yellow]Provider gap:[/yellow] {problem}")


@playbook_app.command("import")
def playbook_import(name: str, path: Path = typer.Argument(..., exists=True)):
    """Load a hand-edited playbook JSON into a campaign."""
    pb = Playbook.model_validate_json(path.read_text())
    _save_campaign(name, pb, "", source="import")
    console.print(f"[green]Imported[/green] playbook for {name}.")


def _campaign_or_exit(s, name: str) -> Campaign:
    c = get_campaign_by_name(s, name)
    if c is None:
        raise typer.BadParameter(f"campaign '{name}' not found. Run `callagent onboard` first.")
    return c


def _print_versions(rows: list[dict]) -> None:
    t = Table(title="Playbook versions (calls run the active one)")
    for col in ("version", "status", "source", "readiness", "blocking questions", "note"):
        t.add_column(col)
    for v in rows:
        style = {"active": "green", "draft": "yellow", "rejected": "red"}.get(v["status"], "dim")
        t.add_row(str(v["version"]), f"[{style}]{v['status']}[/{style}]", v["source"], str(v["readiness_score"]),
                  str(len(v["blocking_questions"])), v["note"][:90])
    console.print(t)


@playbook_app.command("versions")
def playbook_versions(name: str):
    """History of a campaign's playbook."""
    from .training import versions

    with session_scope() as s:
        rows = [versions.describe(v) for v in versions.list_versions(s, _campaign_or_exit(s, name))]
    _print_versions(rows)


@playbook_app.command("approve")
def playbook_approve(
    name: str,
    version: int = typer.Argument(..., help="Version to make live. An older version here is a rollback."),
    force: bool = typer.Option(False, help="Approve even with blocking open questions."),
    by: str = typer.Option("", help="Who is approving."),
):
    """Make a version the one calls run."""
    from .training import versions

    with session_scope() as s:
        try:
            v = versions.describe(versions.approve(s, _campaign_or_exit(s, name), version, by=by, force=force))
        except versions.VersionError as e:
            raise typer.BadParameter(str(e)) from None
    console.print(f"[green]Version {v['version']} is now live[/green] for {name}.")


@playbook_app.command("reject")
def playbook_reject(name: str, version: int, reason: str = typer.Option("", help="Why.")):
    """Turn down a draft. It stays in the history."""
    from .training import versions

    with session_scope() as s:
        try:
            versions.reject(s, _campaign_or_exit(s, name), version, reason=reason)
        except versions.VersionError as e:
            raise typer.BadParameter(str(e)) from None
    console.print(f"Version {version} rejected.")


def _print_documents(rows: list[dict]) -> None:
    t = Table(title="Training documents")
    for col in ("id", "name", "type", "v", "status", "language", "passages", "problem"):
        t.add_column(col)
    for d in rows:
        style = {"ready": "green", "failed": "red"}.get(d["status"], "yellow")
        t.add_row(str(d["id"]), d["name"][:60], d["file_type"], str(d["version"]), f"[{style}]{d['status']}[/{style}]",
                  d["language"] or "", str(d["passages"]), (d["error"] or "")[:80])
    console.print(t)


@train_app.command("add")
def train_add(
    name: str,
    sources: list[str] = typer.Argument(..., help="Files (PDF, DOCX, PPTX, HTML, CSV, TXT, MD) and/or website addresses."),
    max_pages: int = typer.Option(8, help="Pages to read per website."),
):
    """Give the agent documents or a website to learn from. They become searchable on calls
    right away; run `callagent train build` to fold them into the playbook."""
    from .training import service
    from .training.ingest import IngestError

    added: list[int] = []
    for source in sources:
        try:
            with session_scope() as s:
                c = _campaign_or_exit(s, name)
                if source.lower().startswith(("http://", "https://")):
                    doc = service.add_website(s, c, source, max_pages)
                else:
                    path = Path(source)
                    if not path.is_file():
                        raise IngestError("file not found")
                    doc = service.add_file(s, c, path.name, path.read_bytes())
                doc_id = doc.id
            with console.status(f"Reading {source}..."), session_scope() as s:
                service.process_document_sync(s, doc_id)
            added.append(doc_id)
        except IngestError as e:
            console.print(f"[red]{source}:[/red] {e}")
    with session_scope() as s:
        rows = [service.describe(d) for d in service.list_documents(s, _campaign_or_exit(s, name)) if d.id in added]
    if rows:
        _print_documents(rows)


@train_app.command("list")
def train_list(name: str, all_versions: bool = typer.Option(False, "--all", help="Include replaced versions.")):
    """Documents the agent has learned from, and any that could not be read."""
    from .training import service

    with session_scope() as s:
        rows = [service.describe(d) for d in service.list_documents(s, _campaign_or_exit(s, name), all_versions)]
    _print_documents(rows)


@train_app.command("remove")
def train_remove(name: str, document_id: int):
    """Delete a document and everything learned from it."""
    from .training import service

    with session_scope() as s:
        removed = service.remove_document(s, _campaign_or_exit(s, name), document_id)
    console.print("Removed." if removed else "[red]No such document in this campaign.[/red]")


@train_app.command("search")
def train_search(name: str, question: str):
    """What the agent would find for a question during a call."""
    from .db import campaign_organization_id
    from .training.search import load_index

    with session_scope() as s:
        c = _campaign_or_exit(s, name)
        index = load_index(s, campaign_organization_id(s, c), c.id)
        hits = index.search(question)
    if not hits:
        console.print(f"[yellow]Nothing found[/yellow] in {len(index)} passages. On a call the agent would offer a follow-up instead of guessing.")
    for h in hits:
        console.print(Panel(h.text, title=f"{h.source}  (score {h.score})", border_style="cyan"))


@train_app.command("build")
def train_build(name: str):
    """Read the script and every document, and draft a new playbook version for review."""
    from .playbook.analyzer import PlaybookAnalyzer
    from .training import builder, versions

    with console.status("Learning from the script and documents..."), session_scope() as s:
        try:
            v = builder.build_draft(s, _campaign_or_exit(s, name), PlaybookAnalyzer())
        except builder.TrainingError as e:
            raise typer.BadParameter(str(e)) from None
        info, pb = versions.describe(v), Playbook.model_validate(v.playbook_json)
    _print_playbook_summary(pb)
    console.print(f"\n{info['note']}")
    if pb.open_questions:
        console.print("\n[bold]Knowledge gaps[/bold]")
        for q in pb.open_questions:
            console.print(f" - {'[red]*[/red] ' if q.blocking else ''}{q.question}")
    console.print(f"\nDraft saved as [bold]version {info['version']}[/bold]. Calls still run the current version.")
    console.print(f"Review: callagent playbook versions \"{name}\"    Go live: callagent playbook approve \"{name}\" {info['version']}")


@leads_app.command("import")
def leads_import(
    name: str = typer.Argument(..., help="Campaign name."),
    source: str = typer.Argument(..., help="CSV / XLSX path, or a Google Sheets link shared with 'anyone with the link'."),
):
    """Import a lead sheet. Needs a phone column; name/company/email/timezone are picked up if present."""
    from .leads.importer import import_leads

    settings = get_settings()
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            raise typer.BadParameter(f"campaign '{name}' not found")
        report = import_leads(s, c, source, settings.default_phone_region)
    t = Table(title="Import report")
    t.add_column("metric"); t.add_column("value")
    for k, v in report.as_dict().items():
        t.add_row(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
    console.print(t)


@db_app.command("status")
def db_status():
    """Which migration the database is at, and whether a newer one is waiting."""
    from sqlalchemy import create_engine

    from . import migrate

    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        current = migrate.current_revision(connection)
    head = migrate.head_revision()
    console.print(f"database: {current or 'not tracked yet'}   newest migration: {head}")
    if current != head:
        console.print("[yellow]Run `callagent db upgrade` (the server also upgrades when it starts).[/yellow]")


@db_app.command("upgrade")
def db_upgrade():
    """Apply every migration the database has not seen. Back up first: `callagent db backup`."""
    from .db import get_engine

    try:
        get_engine()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print("[green]The database is at the newest migration.[/green]")


@db_app.command("check")
def db_check():
    """List rows that break a uniqueness rule (duplicates a migration would refuse)."""
    from sqlalchemy import create_engine

    from . import migrate

    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        problems = migrate.integrity_problems(connection)
    if not problems:
        console.print("[green]No duplicates found.[/green]")
        return
    for problem in problems:
        console.print(f"[red]- {problem}[/red]")
    raise typer.Exit(1)


@db_app.command("backup")
def db_backup(to: Path = typer.Option(None, help="Directory to write the backup into. Default: DATA_DIR/backups")):
    """Copy the database and uploaded files into a new backup directory. Safe while the server runs."""
    from . import backup

    settings = get_settings()
    try:
        path = backup.create_backup(to or settings.data_dir / "backups", settings)
        manifest = backup.verify_backup(path)
    except backup.BackupError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Backup written and verified:[/green] {path}")
    console.print(f"schema {manifest['schema_revision']}, rows: {manifest['rows']}")
    console.print("Copy this directory off this machine: a backup on the same disk does not survive losing the disk.")


@db_app.command("verify-backup")
def db_verify_backup(path: Path):
    """Check a backup's files against its checksums."""
    from . import backup

    try:
        manifest = backup.verify_backup(path)
    except backup.BackupError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Intact.[/green] Taken {manifest['created_at']}, schema {manifest['schema_revision']}, rows: {manifest['rows']}")


@db_app.command("restore")
def db_restore(path: Path, replace_existing: bool = typer.Option(False, "--replace-existing", help="Overwrite a database that already holds campaigns.")):
    """Replace the database and uploads with a backup. Stop the server first."""
    from . import backup

    try:
        result = backup.restore_backup(path, get_settings(), replace_existing=replace_existing)
    except backup.BackupError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Restored.[/green] Rows now in the database: {result['restored_rows']}")
    console.print("Start the server; it applies any migration newer than the backup.")


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None), reload: bool = False):
    """Run the webhook + media-stream server (expose it publicly, e.g. `ngrok http 8000`)."""
    import uvicorn

    settings = get_settings()
    missing = settings.missing_for_calls()
    if missing:
        console.print(f"[yellow]Missing for live calls: {', '.join(missing)}[/yellow]")
    uvicorn.run("callagent.server:app", host=host or settings.server_host, port=port or settings.server_port, reload=reload)


def _api(method: str, path: str, **kwargs):
    settings = get_settings()
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {resolve_api_token(settings)}"}
    try:
        r = httpx.request(method, settings.server_url + path, timeout=kwargs.pop("timeout", 60), headers=headers, **kwargs)
    except httpx.ConnectError:
        raise typer.Exit(console.print(f"[red]Cannot reach the server at {settings.server_url}. Start it with `callagent serve`.[/red]") or 1)
    if r.status_code >= 400:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        console.print(f"[red]{r.status_code}: {detail}[/red]")
        raise typer.Exit(1)
    return r


def _print_campaign(c: dict) -> None:
    title = f"{c['name']}  (status: {c['status']}, dialer {'running' if c['running'] else 'stopped'}, readiness {c['readiness_score']})"
    if c.get("budget_usd"):
        title += f"  spent ${c.get('spend_usd', 0):.2f} of ${c['budget_usd']:.2f}"
    if c.get("stop_reason"):
        title += f"\nStopped: {c['stop_reason']}"
    t = Table(title=title)
    t.add_column("metric"); t.add_column("count", justify="right")
    for k, v in sorted(c["stats"].items()):
        t.add_row(k, str(v))
    console.print(t)


@campaign_app.command("start")
def campaign_start(name: str, concurrency: int = typer.Option(None), force: bool = typer.Option(False, help="Dial even with blocking open questions.")):
    """Start dialing the campaign's leads."""
    params = {"force": str(force).lower()}
    if concurrency:
        params["concurrency"] = str(concurrency)
    _print_campaign(_api("POST", f"/api/campaigns/{name}/start", params=params).json())


@campaign_app.command("pause")
def campaign_pause(name: str):
    """Stop dialing (calls in progress finish normally)."""
    _print_campaign(_api("POST", f"/api/campaigns/{name}/pause").json())


@campaign_app.command("report")
def campaign_report_cmd(name: str):
    """What the calls say: outcomes, objections, lost reasons, results per playbook version."""
    r = _api("GET", f"/api/campaigns/{name}/report").json()
    calls, cost = r["calls"], r["cost"]
    pct = lambda v: "-" if v is None else f"{v:.1%}"  # noqa: E731
    console.print(Panel.fit(
        f"Calls {calls['total']}   answered {calls['answered']}   conversions {calls['conversions']} ({pct(calls['conversion_rate'])})\n"
        f"Spend ${cost['spend_usd']:.2f}   per conversion {'-' if cost['cost_per_conversion'] is None else '$' + format(cost['cost_per_conversion'], '.2f')}\n"
        f"Leads: hot {r['leads']['hot']}   warm {r['leads']['warm']}   cold {r['leads']['cold']}   unscored {r['leads']['unscored']}",
        title=r["campaign"]))
    if r["objections"]:
        t = Table(title="Objections")
        for col in ("objection", "raised", "handled", "still converted", "example"):
            t.add_column(col)
        for o in r["objections"]:
            t.add_row(o["category"], str(o["raised"]), pct(o["handled_rate"]), pct(o["conversion_rate_when_raised"]), (o["examples"] or [""])[0][:70])
        console.print(t)
    if r["lost_reasons"]:
        console.print("Lost because: " + ", ".join(f"{k} {v}" for k, v in r["lost_reasons"].items()))
    if r["competitors"]:
        console.print("Competitors named: " + ", ".join(f"{k} ({v})" for k, v in r["competitors"].items()))
    t = Table(title="By playbook version" + (f"  (best so far: v{r['best_version']})" if r["best_version"] else ""))
    for col in ("version", "calls", "answered", "conversion rate", "quality", "enough data"):
        t.add_column(col)
    for key, v in r["by_playbook_version"].items():
        t.add_row(key, str(v["calls"]), str(v["answered"]), pct(v["conversion_rate"]), "-" if v["average_quality"] is None else str(v["average_quality"]), "yes" if v["enough_data"] else "no")
    console.print(t)
    if r["unsupported_statements"]:
        console.print(f"[red]{len(r['unsupported_statements'])} statement(s) the playbook does not support[/red]; first: {r['unsupported_statements'][0]['statement'][:100]}")


@campaign_app.command("coach")
def campaign_coach_cmd(name: str):
    """Ask the coach to draft an improved playbook from patterns across the calls."""
    with console.status("Reviewing the campaign's calls..."):
        result = _api("POST", f"/api/campaigns/{name}/coach", timeout=300).json()
    if not result["version"]:
        console.print(f"The coach found no change worth proposing from {result['reviewed_calls']} reviewed calls.")
        return
    for change in result["changes"]:
        console.print(f" - {change['change']}\n   [dim]{change['evidence']}[/dim]")
    console.print(f"\nDraft saved as [bold]version {result['version']}[/bold]. Go live: callagent playbook approve \"{name}\" {result['version']}")


@campaign_app.command("rules")
def campaign_rules_cmd(
    name: str,
    days: str = typer.Option(None, help="Calling days, e.g. mon-fri or mon,wed,fri."),
    hours: str = typer.Option(None, help="Local calling hours, e.g. 09:00-17:30."),
    max_attempts: int = typer.Option(None, help="Dial attempts per lead."),
    retry_minutes: int = typer.Option(None, help="Minutes between attempts."),
    max_per_day: int = typer.Option(None, help="Calls per day."),
    max_per_month: int = typer.Option(None, help="Calls per month."),
    budget: float = typer.Option(None, help="Stop when estimated spend reaches this many dollars."),
    starts: str = typer.Option(None, help="Start date/time with offset, e.g. 2026-12-01T09:00:00-05:00."),
    ends: str = typer.Option(None, help="End date/time with offset."),
    stop_after_meetings: int = typer.Option(None, help="Stop once this many meetings are booked."),
    max_opt_out_rate: float = typer.Option(None, help="Stop above this share of do-not-call requests, e.g. 0.05."),
    clear: bool = typer.Option(False, help="Remove all campaign rules and use the global defaults."),
):
    """Show or change when, how often and how much a campaign may call. Options you give
    are changed; the rest keep their current value."""
    from .dialer.rules import parse_days

    current = _api("GET", f"/api/campaigns/{name}/rules").json()
    changes = {
        "calling_days": parse_days(days) if days else None,
        "window_start": hours.split("-")[0] if hours else None,
        "window_end": hours.split("-")[1] if hours and "-" in hours else None,
        "max_attempts": max_attempts,
        "retry_delay_minutes": retry_minutes,
        "max_calls_per_day": max_per_day,
        "max_calls_per_month": max_per_month,
        "budget_usd": budget,
        "starts_at": starts,
        "ends_at": ends,
        "stop_after_meetings": stop_after_meetings,
        "max_opt_out_rate": max_opt_out_rate,
    }
    changes = {k: v for k, v in changes.items() if v is not None}
    if changes or clear:
        stored = {} if clear else current["stored"]
        current = _api("PUT", f"/api/campaigns/{name}/rules", json={**stored, **changes}).json()
    t = Table(title=f"Calling rules for {name}")
    t.add_column("rule"); t.add_column("value")
    for key, value in current["rules"].items():
        if key != "scoring_weights":
            t.add_row(key.replace("_", " "), "-" if value is None else (", ".join(value) if isinstance(value, list) else str(value)))
    t.add_row("spent so far", f"${current['spend_usd']:.2f}")
    if current.get("stop_reason"):
        t.add_row("stopped because", current["stop_reason"])
    console.print(t)


@campaign_app.command("inbound")
def campaign_inbound(name: str, number: str = typer.Argument(None, help="Your Twilio number in E.164. Omit to stop answering.")):
    """Have this campaign's agent answer calls to one of your numbers.
    Then point that number's "A call comes in" webhook at PUBLIC_BASE_URL/twilio/inbound."""
    c = _api("POST", f"/api/campaigns/{name}/inbound", params={"number": number} if number else {}).json()
    console.print(f"[green]{c['name']}[/green] answers: {c.get('inbound_number') or 'no inbound number'}")


@campaign_app.command("status")
def campaign_status(name: str = typer.Argument(None)):
    """Lead counts by status and dispositions, for one campaign or all."""
    if name:
        _print_campaign(_api("GET", f"/api/campaigns/{name}").json())
    else:
        for c in _api("GET", "/api/campaigns").json():
            _print_campaign(c)


@campaign_app.command("calls")
def campaign_calls(name: str, limit: int = 20):
    """Recent calls with disposition and summary."""
    rows = _api("GET", f"/api/campaigns/{name}/calls", params={"limit": limit}).json()
    t = Table(title="Recent calls")
    for col in CALL_COLUMNS:
        t.add_column(col)
    for r in rows:
        t.add_row(*(str(r.get(col) or "") for col in CALL_COLUMNS))
    console.print(t)


@campaign_app.command("export")
def campaign_export(name: str, out: Path = typer.Argument(Path("results.csv"))):
    """Export every lead with status, disposition, summary, next action and meeting time."""
    out.write_bytes(_api("GET", f"/api/campaigns/{name}/export.csv").content)
    console.print(f"[green]Wrote {out}[/green]")


@app.command("test-call")
def test_call(name: str, phone: str, first_name: str = ""):
    """Place one real call to a number of yours to hear the agent."""
    r = _api("POST", f"/api/campaigns/{name}/dial", params={"phone": phone, "first_name": first_name}).json()
    console.print(f"Dialing... call_run_id={r['call_run_id']}. Watch it with: callagent campaign calls \"{name}\"")


@app.command()
def doctor():
    """Check configuration."""
    settings = get_settings()
    t = Table(title="Configuration")
    t.add_column("setting"); t.add_column("value")
    t.add_row("onboarding model", settings.onboarding_model + f" (effort {settings.onboarding_effort})")
    t.add_row("call model", settings.call_model + f" (effort {settings.call_effort})")
    stt_model = settings.soniox_model if settings.stt_provider == "soniox" else settings.deepgram_model
    t.add_row("STT", f"{settings.stt_provider} {stt_model}")
    t.add_row("TTS", f"{settings.tts_provider} {settings.elevenlabs_model} voice={settings.elevenlabs_voice_id or '-'}")
    t.add_row("inbound fallback campaign", settings.inbound_campaign or "-")
    t.add_row("from number", settings.twilio_from_number or "-")
    t.add_row("public URL", settings.public_base_url or "-")
    t.add_row("calling window", f"{settings.calling_window_start:%H:%M}-{settings.calling_window_end:%H:%M} {settings.default_timezone}")
    t.add_row("database", settings.database_url)
    console.print(t)
    missing = settings.missing_for_calls()
    console.print("[green]Ready for live calls.[/green]" if not missing else f"[yellow]Missing for live calls: {', '.join(missing)}[/yellow]")


if __name__ == "__main__":
    app()


# ----------------------------------------------------------------------------- users


def _org_id(s, ref: str = "") -> int:
    """The organization a command works in: the one named, else the default one."""
    from . import accounts
    from .db import default_organization_id

    if not ref:
        return default_organization_id(s)
    org = accounts.find_organization(s, ref)
    if org is None:
        raise typer.BadParameter(f"no organization '{ref}'. See `callagent orgs list`.")
    return org.id


def _user_or_exit(s, email: str, org: str = ""):
    from . import accounts

    user = accounts.get_user_by_email(s, email)
    if user is None or user.organization_id != _org_id(s, org):
        raise typer.BadParameter(f"no account for {email}" + (f" in organization '{org}'" if org else " in the default organization (use --org)"))
    return user


@users_app.command("add")
def users_add(
    email: str,
    org: str = typer.Option("", "--org", help="Organization name or id. Default: the default organization."),
    name: str = typer.Option("", help="The person's name."),
    role: str = typer.Option("viewer", help="viewer (reads), operator (runs campaigns) or admin (also approves, sets rules, manages users)."),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True, help="At least 10 characters. Asked for when not given."),
):
    """Create an account. The first admin is usually created this way."""
    from . import accounts

    with session_scope() as s:
        try:
            user = accounts.describe(accounts.create_user(s, email, password, name=name, role=role, organization_id=_org_id(s, org)))
        except accounts.AccountError as e:
            raise typer.BadParameter(str(e)) from None
    console.print(f"[green]Created[/green] {user['email']} with the {user['role']} role.")


@users_app.command("list")
def users_list(org: str = typer.Option("", "--org", help="Organization name or id. Default: the default organization.")):
    """Everyone who has an account."""
    from . import accounts

    with session_scope() as s:
        rows = [accounts.describe(u) for u in accounts.list_users(s, _org_id(s, org))]
    table = Table("Email", "Name", "Role", "State", "Last sign-in")
    for u in rows:
        table.add_row(u["email"], u["name"], u["role"], "active" if u["active"] else "deactivated", (u["last_login_at"] or "never")[:16])
    console.print(table)


@users_app.command("role")
def users_role(email: str, role: str, org: str = typer.Option("", "--org", help="Organization the account belongs to. Default: the default organization.")):
    """Change a person's role."""
    from . import accounts

    with session_scope() as s:
        try:
            accounts.update_user(s, _user_or_exit(s, email, org), actor=accounts.operator_principal(_org_id(s, org)), role=role)
        except accounts.AccountError as e:
            raise typer.BadParameter(str(e)) from None
    console.print(f"{email} now has the {role} role.")


@users_app.command("deactivate")
def users_deactivate(email: str, org: str = typer.Option("", "--org", help="Organization the account belongs to. Default: the default organization.")):
    """Stop a person signing in and end their sessions. Their history is kept."""
    from . import accounts

    with session_scope() as s:
        accounts.update_user(s, _user_or_exit(s, email, org), actor=accounts.operator_principal(_org_id(s, org)), active=False)
    console.print(f"{email} can no longer sign in.")


@users_app.command("activate")
def users_activate(email: str, org: str = typer.Option("", "--org", help="Organization the account belongs to. Default: the default organization.")):
    """Let a deactivated person sign in again."""
    from . import accounts

    with session_scope() as s:
        accounts.update_user(s, _user_or_exit(s, email, org), actor=accounts.operator_principal(_org_id(s, org)), active=True)
    console.print(f"{email} can sign in again.")


@users_app.command("password")
def users_password(email: str, password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True)):
    """Set a new password for a person and end their sessions."""
    from . import accounts

    with session_scope() as s:
        try:
            accounts.set_password(s, _user_or_exit(s, email), password)
        except accounts.AccountError as e:
            raise typer.BadParameter(str(e)) from None
    console.print(f"New password set for {email}.")


# ----------------------------------------------------------------------------- organizations


@orgs_app.command("add")
def orgs_add(
    name: str,
    admin_email: str = typer.Option(..., help="Email of the organization's first admin."),
    admin_name: str = typer.Option("", help="Their name."),
    admin_password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True, help="At least 10 characters. Asked for when not given."),
):
    """Create a customer organization and its first admin, who then adds everyone else."""
    from . import accounts

    with session_scope() as s:
        try:
            org, admin = accounts.create_organization(s, name, admin_email=admin_email, admin_password=admin_password, admin_name=admin_name)
        except accounts.AccountError as e:
            raise typer.BadParameter(str(e)) from None
        created = (org.id, org.name, admin.email)
    console.print(f"[green]Created[/green] organization {created[1]} (id {created[0]}) with admin {created[2]}.")


@orgs_app.command("list")
def orgs_list():
    """Every organization on this deployment."""
    from . import accounts

    with session_scope() as s:
        rows = [accounts.describe_organization(s, o) for o in accounts.list_organizations(s)]
    table = Table("Id", "Name", "People", "Active", "Created")
    for o in rows:
        table.add_row(str(o["id"]), o["name"], str(o["users"]), str(o["active_users"]), (o["created_at"] or "")[:10])
    console.print(table)
