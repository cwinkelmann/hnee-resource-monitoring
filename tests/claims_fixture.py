from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
USERS = ("cwinkelmann", "dorian.zwanzig", "andre.kliem")


class FakeUsers:
    def __init__(self, names=USERS):
        self._names = tuple(names)

    def known(self, name):
        return name in self._names

    def all(self):
        return sorted(self._names)


def book(store, user="dorian.zwanzig", gpu=4, gib=40, start=T0, hours=4, now=T0, ip="10.0.0.1",
         note=None, card_mib=81559):
    return store.create(user=user, gpu=gpu, vram_mib=gib * 1024, start=start,
                        end=start + timedelta(hours=hours), note=note, ip=ip, now=now,
                        card_mib=card_mib)
