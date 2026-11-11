import click

from .models import Checkin, Relay, db, hash_token


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables (also done automatically at start-up)."""
        from .migrate import rename_tables
        rename_tables(db)
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

    @app.cli.command("unlock-admin")
    def unlock_admin_cmd():
        """End the pause of the built-in administrator (main.conf) after too many wrong passwords."""
        from flask import current_app
        from .lockout import clear, recent_failures
        key = current_app.config["ADMIN_USERNAME"].strip().lower()
        n = recent_failures(key).count()
        clear(key)
        db.session.commit()
        click.echo(f"Built-in administrator: {n} recent wrong password(s) cleared" if n else
                   "The built-in administrator was not paused")

    @app.cli.command("unlock-user")
    @click.argument("username")
    def unlock_user_cmd(username):
        """Unlock a console account locked after too many wrong passwords."""
        from .accounts import find_user
        from .lockout import unlock
        user = find_user(username)
        if user is None:
            raise click.ClickException(f"No console account named {username}")
        was = unlock(user)
        db.session.commit()
        click.echo(f"{user.username} unlocked" if was else f"{user.username} was not locked")

    @app.cli.command("verify-checkins")
    @click.option("--all", "redo", is_flag=True, help="Re-check every check-in, not only unchecked ones")
    def verify_checkins(redo):
        """Run the server-side face check now (collaborator photos and check-in snapshots)."""
        from flask import current_app
        from .verify import run_once
        engine = current_app.extensions["face_engine"]
        if not engine.wait_ready(600) or not engine.ok:
            raise click.ClickException(engine.message)
        if redo:
            Checkin.query.update({Checkin.verify_status: None})
            db.session.commit()
        collaborators, checkins = run_once()
        flagged = Checkin.query.filter(Checkin.verify_status.in_(Checkin.FLAGGED)).count()
        click.echo(f"{collaborators} collaborator photo(s) and {checkins} check-in(s) checked; {flagged} flagged in total")
