from team import db, simulate


def test_simulated_members_are_flagged(db_file):
    simulate.add_simulated_member(
        "Sim", simulate.PROFILES["Asha (focused dev)"], days=1, seed=1, db_file=db_file
    )

    assert [m["simulated"] for m in db.list_members(db_file)] == [1]
    assert simulate.real_member_count(db_file) == 0


def test_reset_refuses_when_real_members_exist(tmp_path, capsys):
    target = tmp_path / "team.db"
    db.init_db(target)
    db.create_invite("Real person", target)

    assert simulate.main(["--db", str(target), "--reset", "--days", "1"]) == 1
    assert "Refusing to reset" in capsys.readouterr().out
    assert [m["name"] for m in db.list_members(target)] == ["Real person"]


def test_reset_recreates_a_demo_only_db(tmp_path):
    target = tmp_path / "demo.db"

    assert simulate.main(["--db", str(target), "--days", "1"]) == 0
    assert simulate.main(["--db", str(target), "--reset", "--days", "1"]) == 0

    members = db.list_members(target)
    assert len(members) == len(simulate.PROFILES)
    assert all(m["simulated"] for m in members)


def test_default_target_is_not_the_real_team_db():
    from config import TEAM_DB_FILE

    assert simulate.DEMO_DB_FILE != TEAM_DB_FILE
