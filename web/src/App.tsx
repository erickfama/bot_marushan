import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type Track = { title: string; author: string; duration: number; stream: boolean; uri?: string; artwork?: string; requester: string; source: string; position?: number };
type ImportJob = { id: string; status: string; source: string; found: number; resolved: number; omitted: number; added: number; error?: string };
type PlayerState = { connected: boolean; channel?: { id: string; name: string }; playing: boolean; paused: boolean; position: number; volume: number; loop: string; autoplay: boolean; current?: Track; queue: Track[]; historyCount: number; spotify: { configured: boolean }; canControl: boolean; imports: ImportJob[] };
type User = { id: string; name: string; avatar?: string; csrf: string };
type VoiceChannel = { id: string; name: string; members: number; userHere: boolean };

const emptyState: PlayerState = { connected: false, playing: false, paused: false, position: 0, volume: 75, loop: "off", autoplay: false, queue: [], historyCount: 0, spotify: { configured: false }, canControl: false, imports: [] };

export function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
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
    const currentChannel = newState.channel?.id || newChannels.find((item: VoiceChannel) => item.userHere)?.id;
    if (currentChannel) setChannelId(currentChannel);
  }, [request, user]);

  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin" })
      .then(async (response) => { if (!response.ok) throw new Error(); return response.json(); })
      .then((value) => setUser(value))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { refresh().catch((cause) => setError(cause.message)); }, [refresh]);

  useEffect(() => {
    if (!user) return;
    let socket: WebSocket | undefined;
    let retry: number;
    let disposed = false;
    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events`);
      socket.onmessage = () => refresh().catch(() => undefined);
      socket.onclose = (event) => {
        if (disposed) return;
        if (event.code === 4401 || event.code === 4403) {
          setUser(null);
          return;
        }
        retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => { disposed = true; clearTimeout(retry); socket?.close(); };
  }, [refresh, user]);

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
      await request(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "La operación falló"); }
  };

  const submitPlay = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim() || !channelId) return;
    await mutate("/api/play", "POST", { query: query.trim(), channel_id: Number(channelId), next_up: nextUp });
    setQuery("");
  };

  const logout = async () => {
    try {
      await request("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": user?.csrf || "" } });
    } finally {
      location.reload();
    }
  };

  const activeImports = useMemo(() => state.imports.filter((job) => job.status !== "complete" || job.error), [state.imports]);

  if (loading) return <main className="center"><div className="spinner" /><p>Conectando con Bot Marushan…</p></main>;
  if (!user) return <main className="login"><div className="login-card"><span className="brand-mark">N</span><p className="eyebrow">REPRODUCTOR PERSONAL</p><h1>Tu música,<br />en Discord.</h1><p>Controla el canal, importa playlists y mantén la fiesta desde cualquier pantalla.</p><a className="discord-button" href="/auth/discord">Entrar con Discord</a><small>Solo miembros del servidor autorizado.</small></div></main>;

  return <div className="app-shell">
    <header>
      <div className="brand"><span className="brand-mark">M</span><div><strong>Bot Marushan</strong><small>{state.connected ? `En ${state.channel?.name}` : "Sin conexión de voz"}</small></div></div>
      <div className="user"><div><strong>{user.name}</strong><small>{state.canControl ? "Listo para controlar" : "Entra al canal seleccionado"}</small></div>{user.avatar ? <img src={user.avatar} alt="" /> : <span className="avatar">{user.name[0]}</span>}</div>
    </header>

    <main className="layout">
      <section className="player-card">
        <div className="artwork">{state.current?.artwork ? <img src={state.current.artwork} alt="Portada" /> : <div className="vinyl">♪</div>}</div>
        <div className="now-playing">
          <p className="eyebrow">{state.current ? "REPRODUCIENDO AHORA" : "BOT MARUSHAN"}</p>
          <h1>{state.current?.title || "La cola está esperando"}</h1>
          <p className="artist">{state.current?.author || "Agrega una canción o playlist"}</p>
          {state.current?.source === "spotify" && <a className="source-link" href={state.current.uri} target="_blank" rel="noreferrer">Spotify ↗</a>}
          <div className="timeline"><input aria-label="Posición" type="range" min="0" max={state.current?.duration || 1} value={Math.min(position, state.current?.duration || 1)} disabled={!state.canControl || !state.current || state.current.stream} onChange={(event) => setPosition(Number(event.target.value))} onPointerUp={(event) => mutate("/api/settings/seek", "POST", { value: Number(event.currentTarget.value) })} /><div><span>{duration(position)}</span><span>{state.current?.stream ? "EN VIVO" : duration(state.current?.duration || 0)}</span></div></div>
          <div className="controls">
            <button title="Anterior" disabled={!state.canControl} onClick={() => mutate("/api/player/previous")}>↶</button>
            <button className="play" title={state.paused ? "Reanudar" : "Pausar"} disabled={!state.canControl || !state.current} onClick={() => mutate(`/api/player/${state.paused ? "resume" : "pause"}`)}>{state.paused ? "▶" : "Ⅱ"}</button>
            <button title="Siguiente" disabled={!state.canControl} onClick={() => mutate("/api/player/skip")}>↷</button>
            <button title="Detener" disabled={!state.canControl} onClick={() => mutate("/api/player/stop")}>■</button>
          </div>
          <div className="settings-row"><span>Volumen</span><input type="range" min="1" max="150" value={volume} disabled={!state.canControl} onChange={(event) => setVolume(Number(event.target.value))} onPointerUp={(event) => mutate("/api/settings/volume", "POST", { value: Number(event.currentTarget.value) })} /><b>{volume}%</b></div>
        </div>
      </section>

      <aside className="side-card">
        <div className="section-title"><div><p className="eyebrow">DESTINO</p><h2>Canal de voz</h2></div><span className={`status ${state.connected ? "online" : ""}`}>{state.connected ? "Conectado" : "Desconectado"}</span></div>
        <select value={channelId} onChange={(event) => setChannelId(event.target.value)}>{!channelId && <option value="">Selecciona tu canal</option>}{channels.map((channel) => <option key={channel.id} value={channel.id}>{channel.name} · {channel.members}</option>)}</select>
        <button className="secondary full" disabled={!channelId} onClick={() => mutate("/api/connect", "POST", { channel_id: Number(channelId) })}>Conectar al canal</button>
        <div className="divider" />
        <div className="toggle-row"><span>Autoplay</span><button className={state.autoplay ? "active" : ""} onClick={() => mutate("/api/settings/autoplay", "POST", { value: !state.autoplay })}>{state.autoplay ? "Sí" : "No"}</button></div>
        <div className="toggle-row"><span>Repetición</span><select value={state.loop} onChange={(event) => mutate("/api/settings/loop", "POST", { value: event.target.value })}><option value="off">Desactivada</option><option value="track">Canción</option><option value="queue">Cola</option></select></div>
        <div className="service-grid"><span><i className="online" />Discord</span><span><i className={state.spotify.configured ? "online" : ""} />Spotify {state.spotify.configured ? "listo" : "pendiente"}</span></div>
      </aside>

      <section className="queue-card">
        <div className="section-title"><div><p className="eyebrow">A CONTINUACIÓN</p><h2>Cola <span>{state.queue.length}</span></h2></div><div className="queue-actions"><button onClick={() => mutate("/api/queue/shuffle")}>Mezclar</button><button onClick={() => mutate("/api/queue/clear")}>Limpiar</button></div></div>
        <form className="add-form" onSubmit={submitPlay}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Canción, búsqueda o enlace de playlist…" /><label><input type="checkbox" checked={nextUp} onChange={(event) => setNextUp(event.target.checked)} /> Siguiente</label><button disabled={!query.trim() || !channelId}>Agregar</button></form>
        {error && <div className="error">{error}</div>}
        {activeImports.map((job) => <div className={`import ${job.status}`} key={job.id}><div><strong>{job.status === "failed" ? "No se pudo importar" : "Importando playlist"}</strong><span>{job.error || `${job.resolved + job.omitted} de ${job.found || "…"} procesadas`}</span></div><progress max={job.found || 1} value={job.resolved + job.omitted} /></div>)}
        <div className="queue-list">{state.queue.length === 0 ? <div className="empty"><span>♫</span><p>La próxima canción aparecerá aquí.</p></div> : state.queue.map((track) => <article key={`${track.position}-${track.title}`} draggable onDragStart={() => { dragged.current = track.position || null; }} onDragOver={(event) => event.preventDefault()} onDrop={() => { if (dragged.current && track.position && dragged.current !== track.position) mutate("/api/queue/move", "POST", { origin: dragged.current, destination: track.position }); }}><span className="handle">⠿</span><span className="index">{String(track.position).padStart(2, "0")}</span>{track.artwork ? <img src={track.artwork} alt="" /> : <span className="mini-art">♪</span>}<div className="track-copy"><strong>{track.title}</strong><small>{track.author} · Solicitó {track.requester}</small></div><span className="track-duration">{track.stream ? "EN VIVO" : duration(track.duration)}</span><button title="Reproducir ahora" onClick={() => mutate(`/api/queue/jump/${track.position}`)}>▶</button><button title="Eliminar" onClick={() => mutate(`/api/queue/${track.position}`, "DELETE")}>×</button></article>)}</div>
      </section>
    </main>
    <footer><span>Bot Marushan · Audio en Discord</span><button onClick={logout}>Cerrar sesión</button></footer>
  </div>;
}
