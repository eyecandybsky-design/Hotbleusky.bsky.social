# VERVANG in hotbleusky_reposter.py alleen de bestaande process_own_posts()
# door onderstaande functie.
#
# Doel:
# - eigen profiel eerst naar DID resolven
# - verder terug zoeken (max 5 pagina's x 100)
# - alleen eigen originele foto/video-posts
# - geen replies, reposts of quotes
# - laatste 3 geschikte eigen posts
# - alleen actief als nieuwste eigen mediapost <= 24 uur oud is
# - reboost oud -> nieuw
# - GEEN likes op Own Posts
# - Own Posts blijft als laatste stap van de run staan

def process_own_posts():
    print("[OWN POSTS] ophalen via eigen DID...")

    try:
        me = client.get_profile(USERNAME)
        actor = me.did
        print(f"[OWN POSTS] actor: {actor}")
    except Exception as e:
        print(f"OWN POSTS PROFILE ERROR: {type(e).__name__}: {e}")
        return

    eligible = []
    cursor = None
    pages = 0
    max_pages = 5

    while pages < max_pages and len(eligible) < 3:
        try:
            params = {
                "actor": actor,
                "limit": 100,
                "filter": "posts_no_replies",
            }
            if cursor:
                params["cursor"] = cursor

            res = client.app.bsky.feed.get_author_feed(params)
            pages += 1

            print(
                f"[OWN POSTS] pagina {pages}: "
                f"{len(res.feed)} feed-item(s) opgehaald"
            )

            for item in res.feed:
                # Reposts van anderen door HotBleusky nooit als eigen post gebruiken.
                if is_repost_feed_item(item):
                    continue

                p = item.post

                # Extra controle: alleen posts waarvan HotBleusky zelf de auteur is.
                if getattr(p.author, "did", None) != actor:
                    continue

                # Alleen originele foto/video:
                # suitable() sluit replies en quotes uit en vereist media.
                if not suitable(p):
                    continue

                # Dubbele URI's voorkomen.
                if any(existing.uri == p.uri for existing in eligible):
                    continue

                eligible.append(p)

                if len(eligible) >= 3:
                    break

            cursor = getattr(res, "cursor", None)
            if not cursor:
                break

        except Exception as e:
            print(
                f"OWN POSTS FETCH ERROR pagina {pages + 1}: "
                f"{type(e).__name__}: {e}"
            )
            break

    # Nieuwste eerst voor de 24-uurscontrole.
    eligible = sorted(
        eligible,
        key=lambda p: post_datetime(p) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:3]

    print(
        f"[OWN POSTS] {len(eligible)} geschikte eigen originele "
        f"mediapost(s) gevonden na {pages} pagina('s)"
    )

    if not eligible:
        print("OWN POSTS SKIP: geen eigen originele mediapost gevonden")
        return

    newest_dt = post_datetime(eligible[0])

    if not newest_dt:
        print("OWN POSTS SKIP: datum nieuwste eigen post onbekend")
        return

    if newest_dt < now_utc() - timedelta(hours=24):
        print(
            "OWN POSTS SKIP: nieuwste eigen originele mediapost "
            "is ouder dan 24 uur"
        )
        return

    print("[OWN POSTS] laatste 3 reboosten oud -> nieuw")

    # Oudste eerst, nieuwste als allerlaatste actie van de hele run.
    for index, p in enumerate(reversed(eligible), start=1):
        print(f"[OWN POSTS] REBOOST {index}/{len(eligible)}: {p.uri}")
        reboost(p, "Own Posts")
