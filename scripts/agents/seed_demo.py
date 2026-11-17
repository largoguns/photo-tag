"""Fill a local (throw-away) database with demo data to look at every screen.

Usage (never against the real database):
    export DJANGO_DEBUG=1 DATA_DIR=/tmp/phototag-demo DATABASE_URL=sqlite:////tmp/phototag-demo/db.sqlite3
    python manage.py migrate
    python scripts/agents/seed_demo.py
    python scripts/agents/seed_demo.py --many 3000   # plus N extra photos (gallery at scale)

Creates the users demo (superadmin) and maria (collaborator), both with the password
"Demo-Password-1", several synthetic scans with dated photos in different years and a
decade, accepted people and open proposals.
"""

import datetime
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "phototag.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from accounts.models import Role, User  # noqa: E402
from catalog.models import EventType, Face, Photo  # noqa: E402
from catalog.services import voting  # noqa: E402
from scanner.services import pipeline  # noqa: E402

PASSWORD = "Demo-Password-1"


def user(username, role, display_name):
    obj, _ = User.objects.get_or_create(username=username)
    obj.role, obj.display_name, obj.must_change_password = role, display_name, False
    obj.set_password(PASSWORD)
    obj.save()
    return obj


def synthetic_scan(seed):
    rng = np.random.default_rng(seed)
    page = np.full((1754, 1240, 3), 248, np.uint8)
    layout = [
        ((330, 420), (520, 380)),
        ((900, 430), (380, 520)),
        ((330, 1250), (380, 520)),
        ((900, 1250), (520, 380)),
    ]
    for center, size in layout:
        angle = float(rng.uniform(-8, 8))
        box = cv2.boxPoints((center, size, angle)).astype(np.int32)
        cv2.fillPoly(page, [box], [int(c) for c in rng.integers(40, 200, 3)])
    return page


def main():
    if not settings.DEBUG:
        sys.exit("Refusing to seed: set DJANGO_DEBUG=1 and a throw-away DATABASE_URL/DATA_DIR.")
    demo = user("demo", Role.SUPERADMIN, "Demo")
    maria = user("maria", Role.COLLABORATOR, "María")

    inbox = Path(settings.SCANS_INBOX_DIR)
    for directory in (inbox, settings.SCANS_PROCESSED_DIR, settings.THUMBS_DIR):
        Path(directory).mkdir(parents=True, exist_ok=True)
    for seed in range(4):
        path = inbox / f"demo-scan-{seed}.png"
        cv2.imwrite(str(path), synthetic_scan(seed))
        os.utime(path, (0, 0))  # settled
    pipeline.process_inbox()

    dates = [
        (datetime.date(1992, 6, 14), "exact"),
        (datetime.date(1988, 1, 1), "year"),
        (datetime.date(1980, 1, 1), "decade"),
        (None, ""),
    ]
    boda = EventType.objects.get(name="Boda")
    for index, photo in enumerate(Photo.objects.order_by("pk")):
        date, precision = dates[index % len(dates)]
        Photo.objects.filter(pk=photo.pk).update(date_value=date, date_precision=precision)
        face = Face.objects.create(photo=photo, x=60, y=50, w=90, h=110, score=0.9)
        if index % 3 == 0:
            proposal = voting.create_proposal(
                maria, photo, "face_identity", face=face, person_name="carmen lópez de la vega"
            )
            voting.cast_vote(demo, proposal, 1)
            voting.create_proposal(
                maria, photo, "event", event_type=boda, value={"description": "Boda de Luis y Ana"}
            )
        elif index % 3 == 1:
            voting.create_proposal(
                maria,
                photo,
                "face_identity",
                face=face,
                person_name="Un nombre de persona bastante largo para probar desbordes",
            )
    seed_locations(maria, demo)
    seed_family(maria)
    seed_players(maria)
    print(
        f"Seeded {Photo.objects.count()} photos. Users: demo / maria / pablo / lucia, "
        f"password {PASSWORD}"
    )


def seed_locations(maria, demo):
    """Accepted and open locations (photo panel, gallery filter, admin screen)."""
    places = ["Casa de los abuelos", "Playa de Bolonia", "Casa de Los Tarantos"]
    for index, photo in enumerate(Photo.objects.order_by("pk")[:9]):
        proposal = voting.create_proposal(
            maria, photo, "location", location_name=places[index % len(places)]
        )
        if index % 3 != 2:
            try:
                voting.cast_vote(demo, proposal, 1)
            except Exception:  # an identical proposal was already accepted
                continue


def seed_family(maria):
    """Four generations with partners, an ex-partner, honorifics, nicknames and friends."""
    from catalog.models import Person
    from catalog.services import person_details, relationships, voting

    def person(name):
        found = voting.resolve_person(name, maria)
        Person.objects.filter(pk=found.pk).update(is_provisional=False)
        return Person.objects.get(pk=found.pk)

    carmen = person("Carmen López de la Vega")
    pepe = person("José Martín")
    paco, lola, ana = person("Paco Martín"), person("Lola Martín"), person("Ana Ruiz")
    luis, marta, sofia = person("Luis Martín"), person("Marta Martín"), person("Sofía Martín")
    bisabuela = person("Rosario Vega")
    steps = [
        (carmen, "partner", pepe),
        (carmen, "child", paco),
        (pepe, "child", paco),
        (carmen, "child", lola),
        (pepe, "child", lola),
        (paco, "partner", ana),
        (paco, "child", luis),
        (ana, "child", luis),
        (paco, "child", marta),
        (lola, "child", sofia),
        (carmen, "parent", bisabuela),
        (lola, "ex_partner", person("Andrés Gil")),
        (paco, "friend", person("Manolo Pérez")),
        (paco, "compadre", person("Manolo Pérez")),
        (luis, "classmate", person("Jorge Díaz")),
    ]
    for subject, role, other in steps:
        try:
            relationships.add(maria, subject, role, other=other)
        except Exception:  # already there from a previous run
            continue
    relationships.add(maria, marta, "activity_mate", other=sofia, activity="Natación")
    person_details.set_honorific(maria, paco, "Tito")
    person_details.set_honorific(maria, carmen, "Abuela")
    for target, text in ((paco, "Pico pata"), (paco, "Pico Paco"), (luis, "Er Canijo")):
        try:
            person_details.add_nickname(maria, target, text)
        except Exception:
            continue


def seed_players(maria):
    """Two more collaborators who vote, so the ranking and levels have something to show."""
    from catalog.models import Proposal
    from gamification.services import scoring

    pablo = user("pablo", Role.COLLABORATOR, "Pablo")
    lucia = user("lucia", Role.COLLABORATOR, "Lucía")
    for index, proposal in enumerate(Proposal.objects.filter(status=Proposal.Status.OPEN)):
        voter = pablo if index % 2 else lucia
        try:
            voting.cast_vote(voter, proposal, 1)
        except Exception:  # closed meanwhile by a previous vote
            continue
    scoring.rebuild()


def seed_many(count):
    """Extra photo rows reusing the demo files, spread over 60 years (gallery at scale)."""
    import random

    from catalog.models import Scan

    sources = list(Photo.objects.order_by("pk")[:16])
    scan = Scan.objects.create(sha256=f"{count:064d}", file_name=f"many-{count}.png")
    rng = random.Random(count)
    batch = []
    for index in range(count):
        source = sources[index % len(sources)]
        undated = rng.random() < 0.1
        date = None if undated else datetime.date(rng.randint(1950, 2010), rng.randint(1, 12), 1)
        batch.append(
            Photo(
                scan=scan,
                file_name=source.file_name,
                index=index,
                width=source.width,
                height=source.height,
                date_value=date,
                date_precision="" if undated else "year",
            )
        )
    Photo.objects.bulk_create(batch, batch_size=500)
    print(f"Added {count} photos ({Photo.objects.count()} in total).")


if __name__ == "__main__":
    if "--many" in sys.argv:
        seed_many(int(sys.argv[sys.argv.index("--many") + 1]))
    else:
        main()
