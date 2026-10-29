import click

from .models import Checkin, Relay, db, hash_token


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables (also done automatically at start-up)."""
        db.create_all()
        click.echo("Database ready")

    @app.cli.command("create-relay")
    @click.argument("name")
    @click.option("--ips", default="*", help="Allowed IPs/CIDRs, comma separated")
    def create_relay(name, ips):
        """Register a relay and print its token (shown only once)."""
        token = Relay.new_token()
        db.session.add(Relay(name=name, allowed_ips=ips, token_hash=hash_token(token)))
        db.session.commit()
        click.echo(f"Relay '{name}' created. Token: {token}")

    @app.cli.command("verify-checkins")
    @click.option("--all", "redo", is_flag=True, help="Re-check every check-in, not only unchecked ones")
    def verify_checkins(redo):
        """Run the server-side face check now (user photos and check-in snapshots)."""
        from flask import current_app
        from .verify import run_once
        engine = current_app.extensions["face_engine"]
        if not engine.wait_ready(600) or not engine.ok:
            raise click.ClickException(engine.message)
        if redo:
            Checkin.query.update({Checkin.verify_status: None})
            db.session.commit()
        users, checkins = run_once()
        flagged = Checkin.query.filter(Checkin.verify_status.in_(Checkin.FLAGGED)).count()
        click.echo(f"{users} user photo(s) and {checkins} check-in(s) checked; {flagged} flagged in total")
