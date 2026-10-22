"""One spoken message to a short list. At most 100 people."""

from ai_caller.pramaan import client_from_env
from ai_caller.service import Service


def main() -> None:
    service = Service(client_from_env())
    result = service.start_campaign(
        "Delivery notice",
        "+14155550100",
        "Your delivery is ready.",
        [{"id": "notice-1", "to": "+14155550101"}],
    )
    print(result["ref"])


if __name__ == "__main__":
    main()
