from gaia.core.admin import build_parser


def test_parser_accepts_add_user():
    args = build_parser().parse_args(
        ["add-user", "--name", "Ana", "--phone", "13055550001", "--role", "admin"]
    )
    assert args.command == "add-user"
    assert args.name == "Ana"
    assert args.phone == "13055550001"
    assert args.role == "admin"


def test_parser_defaults_role_to_agent():
    args = build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "1305"])
    assert args.role == "agent"


def test_parser_accepts_deactivate():
    args = build_parser().parse_args(["deactivate", "--phone", "13055550001"])
    assert args.command == "deactivate"
