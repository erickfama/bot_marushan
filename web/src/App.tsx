import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type Track = { title: string; author: string; duration: number; duration_ms?: number; stream: boolean; uri?: string; artwork?: string; requester: string; source: string; position?: number; played_at?: number };
type ImportJob = { id: string; query: string; status: string; source: string; found: number; resolved: number; omitted: number; added: number; error?: string; created_at: number };
type PlayerState = { connected: boolean; channel?: { id: string; name: string }; playing: boolean; paused: boolean; position: number; volume: number; loop: string; autoplay: boolean; filter: string; stay247: boolean; current?: Track; currentFavorite: boolean; queue: Track[]; historyCount: number; spotify: { configured: boolean }; canControl: boolean; imports: ImportJob[] };
type User = { id: string; name: string; avatar?: string; csrf: string };
type VoiceChannel = { id: string; name: string; members: number; userHere: boolean };
type Favorite = { uri: string; title: string; author: string; duration_ms: number; artwork?: string; source: string; created_at: number };
type SavedPlaylist = { id: number; name: string; tracks: number; created_at: number };
type Stats = { days: number; plays: number; durationMs: number; artists: number; topTracks: Array<{ title: string; author: string; artwork?: string; plays: number }>; topArtists: Array<{ author: string; plays: number }>; byHour: Array<{ hour: number; plays: number }> };
type Diagnostics = { discord: boolean; discordLatencyMs: number; lavalink: boolean; spotify: boolean; connected: boolean; database: boolean; queueSize: number; importsRunning: number };
type Lyrics = { title: string; artist: string; lines: Array<{ time: number; text: string }>; plain?: string; provider: string };
type Tab = "player" | "lyrics" | "library" | "imports" | "history" | "stats" | "status";

const emptyState: PlayerState = { connected: false, playing: false, paused: false, position: 0, volume: 75, loop: "off", autoplay: false, filter: "off", stay247: false, currentFavorite: false, queue: [], historyCount: 0, spotify: { configured: false }, canControl: false, imports: [] };
const tabs: Array<[Tab, string, string]> = [["player", "Reproductor", "▶"], ["lyrics", "Letras", "≋"], ["library", "Biblioteca", "♥"], ["imports", "Importaciones", "⇩"], ["history", "Historial", "↺"], ["stats", "Estadísticas", "▥"], ["status", "Estado", "●"]];

export function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function dateTime(seconds: number) {
  return new Intl.DateTimeFormat("es-MX", { dateStyle: "medium", timeStyle: "short" }).format(seconds * 1000);
}

function listeningTime(ms: number) {
  const hours = Math.floor(ms / 3_600_000);
  const minutes = Math.floor((ms % 3_600_000) / 60_000);
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [state, setState] = useState<PlayerState>(emptyState);
  const [channels, setChannels] = useState<VoiceChannel[]>([]);
  const [channelId, setChannelId] = useState("");
  const [query, setQuery] = useState("");
  const [nextUp, setNextUp] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [volume, setVolume] = useState(75);
  const [position, setPosition] = useState(0);
  const [tab, setTab] = useState<Tab>("player");
  const [results, setResults] = useState<Track[]>([]);
  const [searching, setSearching] = useState(false);
  const [favorites, setFavorites] = useState<Favorite[]>([]);
  const [playlists, setPlaylists] = useState<SavedPlaylist[]>([]);
  const [playlistName, setPlaylistName] = useState("");
  const [history, setHistory] = useState<Track[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [lyrics, setLyrics] = useState<Lyrics | null>(null);
  const dragged = useRef<number | null>(null);
  const syncedAt = useRef(Date.now());

  const request = useCallback(async (path: string, options: RequestInit = {}) => {
    const headers = new Headers(options.headers);
    if (options.body) headers.set("Content-Type", "application/json");
    if (user?.csrf && (options.method || "GET") !== "GET") headers.set("X-CSRF-Token", user.csrf);
    const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: "La operación falló" }));
      throw new Error(body.detail || "La operación falló");
    }
    return response.status === 204 ? null : response.json();
  }, [user]);

  const refresh = useCallback(async () => {
    if (!user) return;
    const [newState, newChannels] = await Promise.all([request("/api/state"), request("/api/voice-channels")]);
    setState(newState);
    setChannels(newChannels);
    setVolume(newState.volume);
    setPosition(newState.position);
    syncedAt.current = Date.now();
    const selected = newState.channel?.id || newChannels.find((item: VoiceChannel) => item.userHere)?.id;
    if (selected) setChannelId(selected);
  }, [request, user]);

  const loadTab = useCallback(async (selected: Tab) => {
    if (!user) return;
    if (selected === "library") {
      const [savedFavorites, savedPlaylists] = await Promise.all([request("/api/favorites"), request("/api/playlists")]);
      setFavorites(savedFavorites); setPlaylists(savedPlaylists);
    }
    if (selected === "history") setHistory(await request("/api/history?limit=150"));
    if (selected === "lyrics") setLyrics(await request("/api/lyrics"));
    if (selected === "stats") setStats(await request("/api/statistics?days=30"));
    if (selected === "status") setDiagnostics(await request("/api/diagnostics"));
  }, [request, user]);

  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin" })
      .then(async (response) => { if (!response.ok) throw new Error(); return response.json(); })
      .then((value) => setUser(value)).catch(() => setUser(null)).finally(() => setLoading(false));
  }, []);
  useEffect(() => { refresh().catch((cause) => setError(cause.message)); }, [refresh]);
  useEffect(() => { loadTab(tab).catch((cause) => setError(cause.message)); }, [loadTab, tab]);

  useEffect(() => {
    if (!user) return;
    let socket: WebSocket | undefined;
    let retry: number;
    let disposed = false;
    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events`);
      socket.onmessage = () => { refresh().catch(() => undefined); loadTab(tab).catch(() => undefined); };
      socket.onclose = (event) => {
        if (disposed) return;
        if (event.code === 4401 || event.code === 4403) { setUser(null); return; }
        retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => { disposed = true; clearTimeout(retry); socket?.close(); };
  }, [loadTab, refresh, tab, user]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (state.playing && !state.paused && state.current && !state.current.stream) {
        setPosition(Math.min(state.current.duration, state.position + Date.now() - syncedAt.current));
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [state]);

  const mutate = async (path: string, method = "POST", body?: unknown) => {
    try {
      setError("");
      const response = await request(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      await refresh();
      await loadTab(tab);
      return response;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "La operación falló"); }
  };

  const addQuery = async (value: string) => {
    if (!value.trim() || !channelId) return;
    await mutate("/api/play", "POST", { query: value.trim(), channel_id: Number(channelId), next_up: nextUp });
    setQuery(""); setResults([]); setTab("imports");
  };

  const submitPlay = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim() || !channelId) return;
    if (/^https?:\/\//i.test(query.trim())) { await addQuery(query); return; }
    try {
      setSearching(true); setError("");
      setResults(await request(`/api/search?query=${encodeURIComponent(query.trim())}`));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "No se pudo buscar"); }
    finally { setSearching(false); }
  };

  const logout = async () => {
    try { await request("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": user?.csrf || "" } }); }
    finally { location.reload(); }
  };
  const createPlaylist = async (event: FormEvent) => {
    event.preventDefault();
    if (!playlistName.trim()) return;
    await mutate("/api/playlists", "POST", { name: playlistName.trim() });
    setPlaylistName("");
  };
  const activeImports = useMemo(() => [...state.imports].reverse(), [state.imports]);

  if (loading) return <main className="center"><div className="spinner" /><p>Conectando con Bot Nissin…</p></main>;
  if (!user) return <main className="login"><div className="login-card"><span className="brand-mark">N</span><p className="eyebrow">REPRODUCTOR PERSONAL</p><h1>Tu música,<br />en Discord.</h1><p>Controla el canal, importa playlists y conserva tu biblioteca desde cualquier pantalla.</p><a className="discord-button" href="/auth/discord">Entrar con Discord</a><small>Solo miembros del servidor autorizado.</small></div></main>;

  const trackRow = (track: Track | Favorite, action?: () => void) => <article className="library-track" key={`${track.uri}-${track.title}`}>
    {track.artwork ? <img src={track.artwork} alt="" /> : <span className="mini-art">♪</span>}
    <div><strong>{track.title}</strong><small>{track.author}</small></div>
    <span>{duration(("duration_ms" in track ? track.duration_ms : track.duration) || 0)}</span>
    {action && <button onClick={action} disabled={!channelId}>＋</button>}
  </article>;

  return <div className="app-shell">
    <header>
      <div className="brand"><span className="brand-mark">N</span><div><strong>Bot Nissin</strong><small>{state.connected ? `En ${state.channel?.name}` : "Sin conexión de voz"}</small></div></div>
      <nav>{tabs.map(([id, label, icon]) => <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}><span>{icon}</span>{label}</button>)}</nav>
      <div className="user"><div><strong>{user.name}</strong><small>{state.canControl ? "Listo para controlar" : "Entra al canal seleccionado"}</small></div>{user.avatar ? <img src={user.avatar} alt="" /> : <span className="avatar">{user.name[0]}</span>}</div>
    </header>
    {error && <div className="global-error"><span>{error}</span><button onClick={() => setError("")}>×</button></div>}

    {tab === "player" && <main className="layout">
      <section className="player-card"><div className="artwork">{state.current?.artwork ? <img src={state.current.artwork} alt="Portada" /> : <div className="vinyl">♪</div>}</div><div className="now-playing">
        <p className="eyebrow">{state.current ? "REPRODUCIENDO AHORA" : "BOT NISSIN"}</p><h1>{state.current?.title || "La cola está esperando"}</h1><p className="artist">{state.current?.author || "Agrega una canción o playlist"}</p>
        <div className="metadata-actions">{state.current?.source === "spotify" && <a className="source-link" href={state.current.uri} target="_blank" rel="noreferrer">Spotify ↗</a>}<button className={state.currentFavorite ? "liked" : ""} disabled={!state.current} onClick={() => mutate("/api/favorites/current")}>{state.currentFavorite ? "♥ En favoritos" : "♡ Favorito"}</button></div>
        <div className="timeline"><input aria-label="Posición" type="range" min="0" max={state.current?.duration || 1} value={Math.min(position, state.current?.duration || 1)} disabled={!state.canControl || !state.current || state.current.stream} onChange={(event) => setPosition(Number(event.target.value))} onPointerUp={(event) => mutate("/api/settings/seek", "POST", { value: Number(event.currentTarget.value) })} /><div><span>{duration(position)}</span><span>{state.current?.stream ? "EN VIVO" : duration(state.current?.duration || 0)}</span></div></div>
        <div className="controls"><button title="Anterior" disabled={!state.canControl} onClick={() => mutate("/api/player/previous")}>↶</button><button className="play" title={state.paused ? "Reanudar" : "Pausar"} disabled={!state.canControl || !state.current} onClick={() => mutate(`/api/player/${state.paused ? "resume" : "pause"}`)}>{state.paused ? "▶" : "Ⅱ"}</button><button title="Siguiente" disabled={!state.canControl} onClick={() => mutate("/api/player/skip")}>↷</button><button title="Detener" disabled={!state.canControl} onClick={() => mutate("/api/player/stop")}>■</button></div>
        <div className="settings-row"><span>Volumen</span><input type="range" min="1" max="150" value={volume} disabled={!state.canControl} onChange={(event) => setVolume(Number(event.target.value))} onPointerUp={(event) => mutate("/api/settings/volume", "POST", { value: Number(event.currentTarget.value) })} /><b>{volume}%</b></div>
      </div></section>
      <aside className="side-card"><div className="section-title"><div><p className="eyebrow">DESTINO</p><h2>Canal de voz</h2></div><span className={`status ${state.connected ? "online" : ""}`}>{state.connected ? "Conectado" : "Desconectado"}</span></div>
        <select value={channelId} onChange={(event) => setChannelId(event.target.value)}>{!channelId && <option value="">Selecciona tu canal</option>}{channels.map((channel) => <option key={channel.id} value={channel.id}>{channel.name} · {channel.members}</option>)}</select><button className="secondary full" disabled={!channelId} onClick={() => mutate("/api/connect", "POST", { channel_id: Number(channelId) })}>Conectar al canal</button><div className="divider" />
        <div className="toggle-row"><span>Autoplay</span><button className={state.autoplay ? "active" : ""} onClick={() => mutate("/api/settings/autoplay", "POST", { value: !state.autoplay })}>{state.autoplay ? "Sí" : "No"}</button></div><div className="toggle-row"><span>Modo 24/7</span><button className={state.stay247 ? "active" : ""} disabled={!state.connected} onClick={() => mutate("/api/settings/stay247", "POST", { value: !state.stay247 })}>{state.stay247 ? "Sí" : "No"}</button></div><div className="toggle-row"><span>Repetición</span><select value={state.loop} onChange={(event) => mutate("/api/settings/loop", "POST", { value: event.target.value })}><option value="off">Desactivada</option><option value="track">Canción</option><option value="queue">Cola</option></select></div><div className="toggle-row"><span>Filtro</span><select value={state.filter} disabled={!state.canControl} onChange={(event) => mutate("/api/settings/filter", "POST", { value: event.target.value })}><option value="off">Desactivado</option><option value="bassboost">Bass boost</option><option value="nightcore">Nightcore</option><option value="vaporwave">Vaporwave</option><option value="8d">8D</option><option value="karaoke">Karaoke</option></select></div><div className="service-grid"><span><i className="online" />Discord</span><span><i className={state.spotify.configured ? "online" : ""} />Spotify {state.spotify.configured ? "listo" : "pendiente"}</span><span><i className="online" />Biblioteca persistente</span></div>
      </aside>
      <section className="queue-card"><div className="section-title"><div><p className="eyebrow">A CONTINUACIÓN</p><h2>Cola <span>{state.queue.length}</span></h2></div><div className="queue-actions"><button onClick={() => mutate("/api/queue/shuffle")}>Mezclar</button><button onClick={() => mutate("/api/queue/clear")}>Limpiar</button></div></div>
        <form className="add-form" onSubmit={submitPlay}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Busca una canción o pega una playlist…" /><label><input type="checkbox" checked={nextUp} onChange={(event) => setNextUp(event.target.checked)} /> Siguiente</label><button disabled={!query.trim() || !channelId || searching}>{searching ? "Buscando…" : /^https?:\/\//i.test(query) ? "Importar" : "Buscar"}</button></form>
        {results.length > 0 && <div className="search-results"><div className="subheading"><strong>Elige la versión correcta</strong><button onClick={() => setResults([])}>Cerrar</button></div>{results.map((track) => trackRow(track, () => addQuery(track.uri || `${track.title} ${track.author}`)))}</div>}
        <div className="queue-list">{state.queue.length === 0 ? <div className="empty"><span>♫</span><p>La próxima canción aparecerá aquí.</p></div> : state.queue.map((track) => <article key={`${track.position}-${track.title}`} draggable onDragStart={() => { dragged.current = track.position || null; }} onDragOver={(event) => event.preventDefault()} onDrop={() => { if (dragged.current && track.position && dragged.current !== track.position) mutate("/api/queue/move", "POST", { origin: dragged.current, destination: track.position }); }}><span className="handle">⠿</span><span className="index">{String(track.position).padStart(2, "0")}</span>{track.artwork ? <img src={track.artwork} alt="" /> : <span className="mini-art">♪</span>}<div className="track-copy"><strong>{track.title}</strong><small>{track.author} · Solicitó {track.requester}</small></div><span className="track-duration">{track.stream ? "EN VIVO" : duration(track.duration)}</span><button title="Reproducir ahora" onClick={() => mutate(`/api/queue/jump/${track.position}`)}>▶</button><button title="Eliminar" onClick={() => mutate(`/api/queue/${track.position}`, "DELETE")}>×</button></article>)}</div>
      </section>
    </main>}

    {tab === "library" && <main className="page-grid"><div className="library-columns"><section className="page-card wide"><div className="page-heading"><div><p className="eyebrow">TU COLECCIÓN</p><h1>Playlists</h1></div><span>{playlists.length} listas</span></div><form className="playlist-form" onSubmit={createPlaylist}><input value={playlistName} maxLength={80} onChange={(event) => setPlaylistName(event.target.value)} placeholder="Nombre de la nueva playlist" /><button disabled={!playlistName.trim()}>Crear</button></form>{playlists.length ? <div className="playlist-list">{playlists.map((playlist) => <article key={playlist.id}><span className="playlist-cover">♫</span><div><strong>{playlist.name}</strong><small>{playlist.tracks} canciones</small></div><div className="playlist-actions"><button disabled={!channelId || playlist.tracks === 0} onClick={() => mutate(`/api/playlists/${playlist.id}/play`, "POST", { channel_id: Number(channelId), next_up: false })}>Reproducir</button><button disabled={!state.current && !state.queue.length} onClick={() => mutate(`/api/playlists/${playlist.id}/save-queue`)}>Guardar cola</button><button className="danger" onClick={() => mutate(`/api/playlists/${playlist.id}`, "DELETE")}>Eliminar</button></div></article>)}</div> : <div className="empty"><span>♫</span><p>Crea una lista y guarda la cola actual.</p></div>}</section><section className="page-card wide"><div className="page-heading"><div><p className="eyebrow">CANCIONES MARCADAS</p><h1>Favoritos</h1></div><span>{favorites.length} canciones</span></div>{favorites.length ? <div className="library-list">{favorites.map((track) => trackRow(track, () => addQuery(track.uri)))}</div> : <div className="empty large"><span>♡</span><p>Marca canciones desde el reproductor para encontrarlas aquí.</p></div>}</section></div><aside className="tip-card"><p className="eyebrow">BIBLIOTECA LOCAL</p><h2>Tus canciones sobreviven a los reinicios</h2><p>Guarda la canción actual y toda la cola en una playlist propia. Los datos viven en el VPS y no dependen de Spotify.</p></aside></main>}

    {tab === "lyrics" && <main className="lyrics-page"><section className="lyrics-hero">{state.current?.artwork ? <img src={state.current.artwork} alt="" /> : <span className="vinyl">♪</span>}<div><p className="eyebrow">LETRAS SINCRONIZADAS</p><h1>{lyrics?.title || state.current?.title || "Sin reproducción"}</h1><p>{lyrics?.artist || state.current?.author}</p><small>Fuente: {lyrics?.provider || "LRCLIB"}</small></div></section><section className="lyrics-lines">{lyrics?.lines.length ? lyrics.lines.map((line, index) => { const next = lyrics.lines[index + 1]?.time ?? Number.MAX_SAFE_INTEGER; const active = position >= line.time && position < next; return <button className={active ? "active" : ""} key={`${line.time}-${line.text}`} disabled={!state.canControl} onClick={() => mutate("/api/settings/seek", "POST", { value: line.time })}>{line.text}</button>; }) : lyrics?.plain ? <div className="plain-lyrics">{lyrics.plain}</div> : <div className="empty large"><span>≋</span><p>No encontramos letras para esta versión.</p></div>}</section></main>}

    {tab === "imports" && <main className="page-grid"><section className="page-card wide"><div className="page-heading"><div><p className="eyebrow">CENTRO DE IMPORTACIONES</p><h1>Playlists y búsquedas</h1></div><span>{activeImports.length} recientes</span></div><div className="import-list">{activeImports.length ? activeImports.map((job) => <article className={`import-job ${job.status}`} key={job.id}><div className="import-main"><span className="import-icon">{job.status === "complete" ? "✓" : job.status === "failed" ? "!" : job.status === "cancelled" ? "×" : "⇩"}</span><div><strong>{job.query}</strong><small>{dateTime(job.created_at)} · {job.source === "unknown" ? "detectando fuente" : job.source}</small></div><span className="job-status">{job.status === "complete" ? "Completada" : job.status === "failed" ? "Falló" : job.status === "cancelled" ? "Cancelada" : "Procesando"}</span></div><progress max={job.found || 1} value={job.status === "complete" ? job.found || 1 : job.resolved + job.omitted} /><div className="import-detail"><span>{job.found} encontradas</span><span>{job.resolved} resueltas</span><span>{job.omitted} omitidas</span><span>{job.added} agregadas</span></div>{job.error && <p className="job-error">{job.error}</p>}<div className="job-actions">{["queued", "resolving"].includes(job.status) && <button onClick={() => mutate(`/api/imports/${job.id}/cancel`)}>Cancelar</button>}{["failed", "cancelled"].includes(job.status) && <button disabled={!channelId} onClick={() => mutate(`/api/imports/${job.id}/retry`, "POST", { channel_id: Number(channelId) })}>Reintentar</button>}</div></article>) : <div className="empty large"><span>⇩</span><p>Las importaciones aparecerán aquí y sobrevivirán a los reinicios.</p></div>}</div></section><aside className="tip-card"><p className="eyebrow">PROGRESO TRANSPARENTE</p><h2>Sin quedarse pensando</h2><p>Verás cuántas pistas encontró, resolvió, omitió y agregó. Puedes cancelar o reintentar cada trabajo.</p></aside></main>}

    {tab === "history" && <main className="page-grid"><section className="page-card wide"><div className="page-heading"><div><p className="eyebrow">RECIENTEMENTE</p><h1>Historial</h1></div><span>{history.length} reproducciones</span></div><div className="library-list">{history.map((track) => <article className="library-track history-track" key={`${track.played_at}-${track.title}`}>{track.artwork ? <img src={track.artwork} alt="" /> : <span className="mini-art">♪</span>}<div><strong>{track.title}</strong><small>{track.author} · {track.requester}</small></div><span>{track.played_at ? dateTime(track.played_at) : ""}</span><button disabled={!channelId} onClick={() => addQuery(track.uri || `${track.title} ${track.author}`)}>＋</button></article>)}</div></section></main>}

    {tab === "stats" && <main className="stats-page">{stats ? <><section className="metric-grid"><article><span>Reproducciones</span><strong>{stats.plays}</strong><small>últimos {stats.days} días</small></article><article><span>Tiempo escuchado</span><strong>{listeningTime(stats.durationMs)}</strong><small>duración acumulada</small></article><article><span>Artistas</span><strong>{stats.artists}</strong><small>diferentes</small></article><article><span>Hora favorita</span><strong>{stats.byHour.sort((a, b) => b.plays - a.plays)[0]?.hour ?? "—"}:00</strong><small>más actividad</small></article></section><section className="chart-grid"><article className="page-card"><p className="eyebrow">TOP 10</p><h2>Canciones más escuchadas</h2><ol className="ranking">{stats.topTracks.map((track) => <li key={`${track.title}-${track.author}`}><span>{track.artwork ? <img src={track.artwork} alt="" /> : "♪"}</span><div><strong>{track.title}</strong><small>{track.author}</small></div><b>{track.plays}</b></li>)}</ol></article><article className="page-card"><p className="eyebrow">ARTISTAS</p><h2>Más reproducidos</h2><ol className="ranking artists">{stats.topArtists.map((artist, index) => <li key={artist.author}><span>{index + 1}</span><div><strong>{artist.author}</strong><small>{artist.plays} reproducciones</small></div><b>{Math.round((artist.plays / Math.max(stats.plays, 1)) * 100)}%</b></li>)}</ol></article></section></> : <div className="spinner" />}</main>}

    {tab === "status" && <main className="page-grid"><section className="page-card wide"><div className="page-heading"><div><p className="eyebrow">DIAGNÓSTICO</p><h1>Estado del sistema</h1></div><button onClick={() => loadTab("status")}>Actualizar</button></div>{diagnostics && <div className="diagnostic-grid"><Status name="Discord" ok={diagnostics.discord} detail={`${diagnostics.discordLatencyMs} ms`} /><Status name="Lavalink" ok={diagnostics.lavalink} detail="Motor de audio" /><Status name="Spotify" ok={diagnostics.spotify} detail="OAuth y catálogo" /><Status name="Canal de voz" ok={diagnostics.connected} detail={state.channel?.name || "Sin conexión"} /><Status name="Base de datos" ok={diagnostics.database} detail="SQLite persistente" /><Status name="Importaciones" ok={diagnostics.importsRunning === 0} detail={diagnostics.importsRunning ? `${diagnostics.importsRunning} en proceso` : "Sin pendientes"} /></div>}</section><aside className="tip-card"><p className="eyebrow">RESUMEN</p><h2>{diagnostics?.queueSize || 0} canciones en cola</h2><p>Esta vista separa problemas de Discord, audio, Spotify y almacenamiento para localizar fallos sin buscar primero en los logs.</p></aside></main>}

    <footer><span>Bot Nissin · Audio en Discord · Datos guardados localmente</span><button onClick={logout}>Cerrar sesión</button></footer>
  </div>;
}

function Status({ name, ok, detail }: { name: string; ok: boolean; detail: string }) {
  return <article className="diagnostic"><i className={ok ? "online" : ""} /><div><strong>{name}</strong><small>{detail}</small></div><span>{ok ? "Operativo" : "Atención"}</span></article>;
}
