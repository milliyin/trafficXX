import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Player = {
  id: string;
  nickname: string;
  score: number;
  bio: string;
  website: string;
  account: boolean;
  csrf: string;
  ad_hidden: boolean;
  image_url: string | null;
};
type Entry = Pick<
  Player,
  "id" | "nickname" | "score" | "bio" | "website" | "image_url" | "ad_hidden"
>;
type Board = {
  entries: Entry[];
  totals: { players: number; clicks: number };
  paused: boolean;
  offset: number;
};
class ApiError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    super(code);
  }
}
const messages: Record<string, string> = {
  SERVER_AT_CAPACITY:
    "The server is full for a moment. Your click was not counted. Try again.",
  DATABASE_BUSY: "All database slots are busy. Try again in a moment.",
  GAME_PAUSED: "The game is paused. Scores are safe.",
  INVALID_CREDENTIALS: "That email and passphrase do not match.",
  SIGNUP_UNAVAILABLE: "That email cannot be registered. Try signing in.",
  AUTH_RATE_LIMITED: "Too many account requests. Please wait a minute.",
  AUTH_BUSY: "Account processing is busy. Please try again in a moment.",
  INVALID_IMAGE: "Use a still PNG, JPEG or WebP, at most 2048 × 2048 pixels.",
  IMAGE_TOO_LARGE: "Choose an image smaller than 1 MB.",
  INVALID_WEBSITE: "Use a full http:// or https:// website address.",
  ACCOUNT_REQUIRED: "Create an account first.",
  SESSION_EXPIRED: "Your session expired. Please reload and sign in.",
};
const number = (value: number) => new Intl.NumberFormat("en").format(value);
async function api<T>(
  path: string,
  method = "GET",
  data?: unknown,
  csrf?: string,
): Promise<T> {
  const raw = data instanceof File;
  let response: Response;
  try {
    response = await fetch("/api" + path, {
      method,
      credentials: "same-origin",
      signal: AbortSignal.timeout(10000),
      headers: {
        ...(data !== undefined
          ? {
              "Content-Type": raw
                ? "application/octet-stream"
                : "application/json",
            }
          : {}),
        ...(csrf ? { "X-CC-CSRF": csrf } : {}),
      },
      body: data === undefined ? undefined : raw ? data : JSON.stringify(data),
    });
  } catch {
    throw new Error(
      "Connection interrupted. If a click is unconfirmed, retry it with the button below.",
    );
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new ApiError(
      response.status >= 500 ? "SERVER_AT_CAPACITY" : "REQUEST_FAILED",
      response.status,
    );
  }
  if (!response.ok)
    throw new ApiError(
      result.detail?.code || "REQUEST_FAILED",
      response.status,
    );
  return result;
}
function explain(error: unknown) {
  return error instanceof ApiError
    ? messages[error.code] || "Something went wrong. Please try again."
    : error instanceof Error
      ? error.message
      : "Something went wrong.";
}

function App() {
  const path = location.pathname;
  const [me, setMe] = useState<Player | null>(null);
  const [boot, setBoot] = useState(true);
  const [notice, setNotice] = useState("");
  const [board, setBoard] = useState<Board | null>(null);
  const [boardError, setBoardError] = useState("");
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        let player: Player;
        try {
          player = await api<Player>("/me");
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 401) throw error;
          if (path !== "/play" && path !== "/account") return;
          await api("/guest", "POST", {});
          player = await api<Player>("/me");
        }
        if (active) setMe(player);
      } catch (error) {
        if (active) setNotice(explain(error));
      } finally {
        if (active) setBoot(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    let active = true,
      busy = false;
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const data = await api<Board>("/leaderboard?offset=" + offset);
        if (active) {
          setBoard(data);
          setBoardError("");
          setLastChecked(new Date());
        }
      } catch (error) {
        if (active) setBoardError(explain(error));
      } finally {
        busy = false;
      }
    };
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 10000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [offset, refresh]);
  async function reloadMe() {
    const player = await api<Player>("/me");
    setMe(player);
    setRefresh((n) => n + 1);
  }
  return (
    <>
      <header className="nav">
        <a className="brand" href="/" aria-label="ClickClash home">
          <span className="brand-icon">↗</span>ClickClash
          <span className="brand-dot">.</span>
        </a>
        <nav aria-label="Main navigation">
          <a className={path === "/" ? "active" : ""} href="/">
            Leaderboard
          </a>
          <a className={path === "/play" ? "active" : ""} href="/play">
            Play the game
          </a>
          <a className={path === "/status" ? "active" : ""} href="/status">
            Status <span className="status-dot muted" />
          </a>
        </nav>
        <a className="account-link" href="/account">
          {me?.account ? me.nickname : "Join the clash"} <span>↗</span>
        </a>
      </header>
      <main>
        {notice && (
          <div role="alert" className="notice">
            {notice} <button onClick={() => location.reload()}>Reload</button>
          </div>
        )}
        {path === "/play" ? (
          <Play
            me={me}
            setMe={setMe}
            boot={boot}
            paused={board?.paused ?? false}
          />
        ) : path === "/account" ? (
          <Account me={me} boot={boot} reloadMe={reloadMe} />
        ) : path === "/status" ? (
          <Status />
        ) : (
          <>
            <section className="hero">
              <div>
                <div className="eyebrow">
                  <span className="tiny-star">✳</span> ONE BUTTON. ALL THE
                  GLORY.
                </div>
                <h1>
                  Big clicks.
                  <br />
                  Bigger <span className="outlined">bragging rights.</span>
                </h1>
                <p>
                  A little friendly competition. An unreasonable amount of
                  clicking.
                  <br className="desktop-break" /> Climb the ranks. Give your
                  thing a little spotlight.
                </p>
                <a className="button dark" href="/play">
                  Make your move <span>↗</span>
                </a>
              </div>
              <div className="hero-sticker" aria-hidden="true">
                <span>
                  YOUR FINGER.
                  <br />
                  YOUR LEGACY.
                </span>
                <div className="pointer">↖</div>
                <small>NO TIMER. NO FINISH LINE.</small>
              </div>
            </section>
            <section className="stats-strip" aria-label="All-time statistics">
              <div>
                <span className="stat-dot" />
                THE CLASH SO FAR
              </div>
              <div>
                <strong>{board ? number(board.totals.clicks) : "—"}</strong>
                <span>accepted clicks</span>
              </div>
              <div>
                <strong>{board ? number(board.totals.players) : "—"}</strong>
                <span>ranked players</span>
              </div>
              <div>
                <strong>∞</strong>
                <span>bragging rights</span>
              </div>
            </section>
            <section className="board-section">
              <div className="section-head">
                <div>
                  <div className="eyebrow">THE PEOPLE MAKING NOISE</div>
                  <h2>
                    The leaderboard<span>.</span>
                  </h2>
                </div>
                <div className="live-label">
                  <span
                    className={boardError ? "status-dot muted" : "status-dot"}
                  />
                  {boardError
                    ? "Updates unavailable"
                    : "Refreshes every 10 seconds"}
                  <small>
                    {lastChecked &&
                      "Last checked " + lastChecked.toLocaleTimeString()}
                  </small>
                </div>
              </div>
              <div className="board-tabs">
                <span className="tab-selected">All time</span>
                <span>No resets. Every accepted click stays.</span>
              </div>
              {boardError && (
                <div role="alert" className="notice">
                  {boardError} Showing the last available results.
                </div>
              )}
              <div className="board-head">
                <span>RANK</span>
                <span>PLAYER</span>
                <span>CLICKS</span>
                <span>THEIR CORNER OF THE INTERNET</span>
              </div>
              {!board && !boardError && (
                <div className="empty">Loading the clash…</div>
              )}
              {board && board.entries.length === 0 && (
                <div className="empty">
                  <span className="empty-mark">↗</span>
                  <h3>The top spot is open.</h3>
                  <p>
                    Be the first to put your name here. Create an account to
                    rank.
                  </p>
                  <a href="/play" className="button dark">
                    Go get it ↗
                  </a>
                </div>
              )}
              {board?.entries.map((entry, index) => (
                <div
                  className={"board-row rank-" + (offset + index + 1)}
                  key={entry.id}
                >
                  <div className="rank">
                    {String(offset + index + 1).padStart(2, "0")}
                    {offset + index === 0 && <span className="crown">♛</span>}
                  </div>
                  <div className="player-cell">
                    <span className={"avatar color-" + (index % 4)}>
                      {entry.nickname.slice(0, 2).toUpperCase()}
                    </span>
                    <div>
                      <strong>{entry.nickname}</strong>
                      <small>
                        {offset + index === 0
                          ? "Currently setting the pace"
                          : "In it for the clicks"}
                      </small>
                    </div>
                  </div>
                  <div className="score-cell">
                    {number(entry.score)}
                    <small>clicks</small>
                  </div>
                  <div className="ad-cell">
                    {entry.image_url && (
                      <img
                        src={entry.image_url}
                        alt={entry.nickname + " promotion"}
                        loading="lazy"
                      />
                    )}
                    <div>
                      <p>
                        {entry.bio ||
                          (entry.ad_hidden
                            ? "Promotion hidden by moderator."
                            : "Just here for the glory.")}
                      </p>
                      {entry.website && (
                        <a
                          href={entry.website}
                          target="_blank"
                          rel="ugc sponsored nofollow noopener noreferrer"
                        >
                          Visit my corner ↗
                        </a>
                      )}
                    </div>
                    <button
                      className="report"
                      title="Report this promotion"
                      aria-label={"Report " + entry.nickname}
                      onClick={async () => {
                        if (!me) {
                          setNotice(
                            "Sign in or play as a guest before reporting a profile.",
                          );
                          return;
                        }
                        const reason = prompt(
                          "Why should we review this profile?",
                        );
                        if (!reason) return;
                        try {
                          await api(
                            "/report/" + entry.id,
                            "POST",
                            { reason },
                            me.csrf,
                          );
                          setNotice("Report sent. Thank you.");
                        } catch (error) {
                          setNotice(explain(error));
                        }
                      }}
                    >
                      ⚑
                    </button>
                  </div>
                </div>
              ))}
              <div className="pagination">
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset((n) => Math.max(0, n - 25))}
                >
                  ← Previous
                </button>
                <span>
                  {board
                    ? number(board.totals.players) +
                      " players. One leaderboard."
                    : "All-time rankings"}
                </span>
                <button
                  disabled={
                    !board ||
                    offset + 25 >= board.totals.players ||
                    offset >= 1000
                  }
                  onClick={() => setOffset((n) => n + 25)}
                >
                  Next →
                </button>
              </div>
            </section>
            <section className="bottom-callout">
              <div>
                <h2>Your next click could be iconic.</h2>
                <p>
                  Play as a guest. Make an account to save your place on the
                  board.
                </p>
              </div>
              <a className="button dark" href="/play">
                Let’s make some noise ↗
              </a>
            </section>
          </>
        )}
      </main>
      <footer>
        <a className="brand small" href="/">
          ClickClash.
        </a>
        <p>A tiny button. A very real competition.</p>
        <a href="/status">Service status ↗</a>
        <span>Built on trafficXX</span>
      </footer>
    </>
  );
}

function Play({
  me,
  setMe,
  boot,
  paused,
}: {
  me: Player | null;
  setMe: React.Dispatch<React.SetStateAction<Player | null>>;
  boot: boolean;
  paused: boolean;
}) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(0);
  const [accepted, setAccepted] = useState(0);
  const [auto, setAuto] = useState(false);
  const [uncertain, setUncertain] = useState<string[]>([]);
  const inFlight = useRef(0);
  const clickRef = useRef<() => void>(() => {});
  useEffect(() => {
    if (!me) return;
    try {
      const saved = JSON.parse(
        sessionStorage.getItem("cc-pending-" + me.id) || "[]",
      );
      if (Array.isArray(saved))
        setUncertain(saved.filter((x) => typeof x === "string").slice(0, 64));
    } catch {
      /* Ignore invalid browser-local pending data. */
    }
  }, [me?.id]);
  function remember(id: string, add: boolean) {
    setUncertain((old) => {
      const next = add
        ? [...new Set([...old, id])].slice(-64)
        : old.filter((x) => x !== id);
      if (me)
        sessionStorage.setItem("cc-pending-" + me.id, JSON.stringify(next));
      return next;
    });
  }
  async function send(id: string = crypto.randomUUID()) {
    if (!me || inFlight.current >= 8 || paused) return;
    inFlight.current++;
    setPending(inFlight.current);
    remember(id, true);
    try {
      const result = await api<{ score: number; duplicate: boolean }>(
        "/click",
        "POST",
        { id },
        me.csrf,
      );
      setMe((old) =>
        old ? { ...old, score: Math.max(old.score, result.score) } : old,
      );
      if (!result.duplicate) setAccepted((n) => n + 1);
      remember(id, false);
      setError("");
    } catch (e) {
      setError(explain(e));
      setAuto(false);
      if (e instanceof ApiError && e.status < 500) remember(id, false);
    } finally {
      inFlight.current--;
      setPending(inFlight.current);
    }
  }
  clickRef.current = () => {
    void send();
  };
  useEffect(() => {
    if (!auto) return;
    const timer = setInterval(() => clickRef.current(), 100);
    return () => clearInterval(timer);
  }, [auto]);
  return (
    <section className="play-page">
      <div className="eyebrow">LESS TALK. MORE CLICK.</div>
      <h1>
        Make it <span className="outlined">count.</span>
      </h1>
      <p>No timer. No finish line. Just you and the button.</p>
      {!me?.account && (
        <div className="guest-banner">
          You’re playing as a guest. <a href="/account">Create an account</a> to
          keep a leaderboard entry. Clearing cookies loses guest access.
        </div>
      )}
      <div className="play-layout">
        <div className="click-arena">
          <div className="arena-top">
            <span>{me?.nickname || "Connecting…"}</span>
            <span>{paused ? "GAME PAUSED" : "ALL-TIME CLICKS"}</span>
          </div>
          <div className="own-score" aria-live="polite">
            {me ? number(me.score) : "—"}
          </div>
          <button
            className="click-button"
            disabled={!me || boot || paused}
            onClick={() => void send()}
          >
            <span>CLICK</span>
            <small>MAKE SOME NOISE ↗</small>
          </button>
          <div className="arena-bottom">
            <span>{pending} in flight</span>
            <span>{accepted} confirmed this visit</span>
          </div>
        </div>
        <aside className="rules-card">
          <span className="mini-tag">THE VERY SHORT RULEBOOK</span>
          <h2>
            One click.
            <br />
            One point.
          </h2>
          <p>
            Only clicks accepted by the server earn points. Refreshing won’t
            erase them.
          </p>
          <hr />
          <h3>Robots welcome.</h3>
          <p>
            Automatic clickers are allowed. Everyone shares a fixed server
            capacity. When it’s full, excess requests are rejected.
          </p>
          <label className="auto-toggle">
            <input
              type="checkbox"
              checked={auto}
              disabled={!me || paused}
              onChange={(e) => setAuto(e.target.checked)}
            />{" "}
            Try auto-clicking <small>10 requests/second from this tab</small>
          </label>
          <a href="/" className="text-link">
            See who’s ahead ↗
          </a>
        </aside>
      </div>
      {error && (
        <div role="alert" className="notice">
          {error}
        </div>
      )}
      {uncertain.length > 0 && (
        <div className="retry-bar">
          {uncertain.length} unconfirmed request(s). Retrying the original IDs
          cannot score twice.{" "}
          <button
            disabled={pending > 0}
            onClick={() => {
              for (const id of uncertain.slice(0, 8)) void send(id);
            }}
          >
            Retry unconfirmed
          </button>
        </div>
      )}
    </section>
  );
}

function Account({
  me,
  boot,
  reloadMe,
}: {
  me: Player | null;
  boot: boolean;
  reloadMe: () => Promise<void>;
}) {
  const [mode, setMode] = useState<"signup" | "login">("signup");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [saved, setSaved] = useState(false);
  async function auth(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!me) return;
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      if (mode === "signup") {
        const result = await api<{ passphrase: string }>(
          "/signup",
          "POST",
          {
            email: form.get("email"),
            nickname: form.get("nickname"),
            accept_no_recovery: form.get("accept") === "on",
          },
          me.csrf,
        );
        setPhrase(result.passphrase);
      } else {
        await api(
          "/login",
          "POST",
          { email: form.get("email"), passphrase: form.get("passphrase") },
          me.csrf,
        );
      }
      await reloadMe();
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }
  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!me) return;
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      await api(
        "/profile",
        "PUT",
        {
          nickname: form.get("nickname"),
          bio: form.get("bio"),
          website: form.get("website"),
        },
        me.csrf,
      );
      await reloadMe();
      setSaved(true);
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="account-page">
      <div className="eyebrow">YOUR CORNER OF THE CLASH</div>
      <h1>{me?.account ? "Make it yours." : "Put your name on it."}</h1>
      <p>Your clicks. Your profile. Your little piece of the leaderboard.</p>
      {error && (
        <div role="alert" className="notice">
          {error}
        </div>
      )}
      {phrase && (
        <div className="passphrase-panel">
          <h2>Save this. Seriously.</h2>
          <p>
            This is your generated passphrase. It is shown once and cannot be
            recovered.
          </p>
          <output>{phrase}</output>
          <button
            className="button dark"
            onClick={() =>
              void navigator.clipboard
                .writeText(phrase)
                .catch(() =>
                  setError(
                    "Copy is unavailable. Please select and save the passphrase manually.",
                  ),
                )
            }
          >
            Copy passphrase
          </button>
          <p>
            Sign in using your email and these five words. There are no reset
            codes or recovery emails.
          </p>
          <button onClick={() => setPhrase("")}>I saved it — hide it</button>
        </div>
      )}
      {boot ? (
        <p>Connecting…</p>
      ) : !me ? (
        <p>Could not establish a session. Reload to try again.</p>
      ) : me.account ? (
        <form className="profile-form" onSubmit={save} key={me.id}>
          <div className="form-card">
            <h2>Your public profile</h2>
            <label>
              Public nickname
              <input
                name="nickname"
                required
                minLength={2}
                maxLength={32}
                defaultValue={me.nickname}
              />
            </label>
            <label>
              Your bio / little ad
              <textarea
                name="bio"
                maxLength={180}
                rows={3}
                defaultValue={me.bio}
                placeholder="Tell the internet about your thing."
              />
            </label>
            <label>
              Website link
              <input
                name="website"
                type="url"
                maxLength={500}
                defaultValue={me.website}
                placeholder="https://your-corner.com"
              />
            </label>
            <p className="form-note">
              Your email is private. Your nickname, bio, link and image appear
              publicly. Ads publish immediately and may be hidden or removed by
              the operator.
            </p>
            {me.ad_hidden && (
              <p className="notice">
                Your promotion is currently hidden by a moderator.
              </p>
            )}
            <button className="button dark" disabled={busy}>
              {busy ? "Saving…" : "Save profile ↗"}
            </button>
            {saved && (
              <span role="status" className="saved">
                Saved.
              </span>
            )}
          </div>
          <div className="form-card">
            <h2>A picture for your pitch</h2>
            {me.image_url && (
              <img
                className="profile-image"
                src={me.image_url + "?v=" + Date.now()}
                alt="Your promotion"
              />
            )}
            <label>
              Choose an image
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                disabled={busy}
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  setBusy(true);
                  try {
                    await api("/profile/image", "PUT", file, me.csrf);
                    await reloadMe();
                    setError("");
                  } catch (error) {
                    setError(explain(error));
                  } finally {
                    setBusy(false);
                    e.target.value = "";
                  }
                }}
              />
            </label>
            <p className="form-note">
              PNG, JPEG or still WebP. Up to 1 MB and 2048 × 2048 pixels. We
              resize and remove metadata.
            </p>
            {me.image_url && (
              <button
                type="button"
                onClick={async () => {
                  try {
                    await api("/profile/image", "DELETE", undefined, me.csrf);
                    await reloadMe();
                  } catch (e) {
                    setError(explain(e));
                  }
                }}
              >
                Remove image
              </button>
            )}
            <hr />
            <p>
              Your all-time score: <strong>{number(me.score)}</strong>
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                try {
                  await api("/logout", "POST", {}, me.csrf);
                  location.assign("/");
                } catch (e) {
                  setError(explain(e));
                }
              }}
            >
              Sign out
            </button>
            <p className="form-note">
              Keep your passphrase. Without it, you cannot sign back in.
            </p>
          </div>
        </form>
      ) : (
        <div className="auth-layout">
          <form className="form-card" onSubmit={auth}>
            <div className="auth-tabs">
              <button
                type="button"
                className={mode === "signup" ? "selected" : ""}
                onClick={() => setMode("signup")}
              >
                Create account
              </button>
              <button
                type="button"
                className={mode === "login" ? "selected" : ""}
                onClick={() => setMode("login")}
              >
                Sign in
              </button>
            </div>
            <label>
              Email
              <input
                name="email"
                type="email"
                required
                maxLength={254}
                autoComplete="username"
              />
            </label>
            {mode === "signup" ? (
              <>
                <label>
                  Public nickname
                  <input
                    name="nickname"
                    required
                    minLength={2}
                    maxLength={32}
                    defaultValue={me.nickname}
                  />
                </label>
                <label className="check-label">
                  <input type="checkbox" name="accept" required />I understand:
                  my generated passphrase is shown once. There is no password
                  recovery.
                </label>
              </>
            ) : (
              <label>
                Your passphrase
                <input
                  name="passphrase"
                  type="password"
                  required
                  maxLength={256}
                  autoComplete="current-password"
                  placeholder="word-word-word-word-word"
                />
              </label>
            )}
            <button disabled={busy} className="button dark">
              {busy
                ? "One moment…"
                : mode === "signup"
                  ? "Claim my spot ↗"
                  : "Welcome back ↗"}
            </button>
            <p className="form-note">
              No verification codes. Your email is an unverified sign-in name,
              not displayed publicly.
            </p>
          </form>
          <aside className="auth-aside">
            <span>✳</span>
            <h2>
              Less paperwork.
              <br />
              More clicking.
            </h2>
            <p>
              We generate a five-word passphrase for you. Save it somewhere
              safe. Your guest clicks join your account when you sign up or sign
              in.
            </p>
          </aside>
        </div>
      )}
      {me?.account && <AccountDeletion me={me} />}
    </section>
  );
}

function AccountDeletion({ me }: { me: Player }) {
  const [error, setError] = useState("");
  return (
    <div className="form-note">
      <p>
        You can permanently remove your account, public profile and score.
        Anonymized click receipts remain to prevent duplicate scoring.
      </p>
      <button
        type="button"
        onClick={async () => {
          if (
            !confirm(
              "Permanently delete your account, profile and score? This cannot be undone.",
            )
          )
            return;
          try {
            await api("/account", "DELETE", undefined, me.csrf);
            location.assign("/");
          } catch (e) {
            setError(explain(e));
          }
        }}
      >
        Delete my account
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

function Status() {
  const [data, setData] = useState<{
    checked_at: string;
    components: { name: string; ok: boolean }[];
    history: { checked_at: string; ok: boolean }[];
  } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const response = await fetch(
          "https://milliyin.github.io/trafficXX/public-status/history.json",
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error();
        const result = await response.json();
        if (!result.checked_at || !Array.isArray(result.components))
          throw new Error();
        if (active) {
          setData(result);
          setError("");
        }
      } catch {
        if (active)
          setError("External monitoring has not published a current snapshot.");
      }
    }
    void load();
    const timer = setInterval(() => void load(), 60000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  const age = data ? Date.now() - Date.parse(data.checked_at) : NaN;
  const stale =
    !data || !Number.isFinite(age) || age < -60000 || age > 15 * 60000;
  return (
    <section className="status-page">
      <div className="eyebrow">NO GUESSWORK. JUST CHECKS.</div>
      <h1>How’s the clash?</h1>
      <div className={"status-banner " + (stale ? "unknown" : "")}>
        <span className="status-dot" />
        {stale
          ? "Status unknown"
          : data?.components.every((x) => x.ok)
            ? "All monitored components operational"
            : "Service disruption detected"}
      </div>
      <p>
        {data
          ? "Last external check: " + new Date(data.checked_at).toLocaleString()
          : "Monitoring history starts when external checks are enabled."}
      </p>
      {error && <p>{error}</p>}
      {data?.components.map((component) => (
        <div className="component" key={component.name}>
          <strong>{component.name}</strong>
          <span>
            {stale ? "Unknown" : component.ok ? "Operational" : "Unavailable"}
          </span>
        </div>
      ))}
      <h2>Check history</h2>
      <div className="uptime-bars">
        {data?.history.slice(-90).map((check, index) => (
          <span
            key={index}
            className={check.ok ? "ok" : "down"}
            title={
              new Date(check.checked_at).toLocaleString() +
              (check.ok ? " — passed" : " — failed")
            }
          />
        ))}
      </div>
      <p className="form-note">
        Each bar is an actual recorded check, not a day of assumed uptime.
        Missing or stale evidence is unknown. This in-app view can also become
        unreachable during an outage; use the independently hosted status page
        when configured.
      </p>
      <a
        className="text-link"
        href="https://milliyin.github.io/trafficXX/public-status/"
        target="_blank"
        rel="noopener noreferrer"
      >
        Open independent status page ↗
      </a>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
