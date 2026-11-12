import React, { useEffect, useState } from 'react';
import { Pause as PauseIcon, Play as PlayIcon, Volume2 as VolumeIcon, VolumeX as MutedIcon } from 'lucide-react';

const clock = (seconds: number) => {
    const s = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/**
 * Upright controls for a video the user has rotated. The browser's own controls
 * live inside the <video> and would turn sideways with it, so a turned video
 * hides them and gets these instead: a centred play button while paused (sized
 * to cover the play icon baked into the poster, which turns with the picture)
 * and a bar with play/pause, a scrubber, the time and mute.
 */
export const TurnedVideoControls: React.FC<{
    video: React.RefObject<HTMLVideoElement | null>;
    /** Diameter of the centred play button, in px. */
    discSize: number;
}> = ({ video, discSize }) => {
    const [playing, setPlaying] = useState(false);
    const [time, setTime] = useState(0);
    const [duration, setDuration] = useState(0);
    const [muted, setMuted] = useState(false);

    useEffect(() => {
        const el = video.current;
        if (!el) return undefined;
        const sync = () => {
            setPlaying(!el.paused && !el.ended);
            setTime(el.currentTime);
            setDuration(Number.isFinite(el.duration) ? el.duration : 0);
            setMuted(el.muted);
        };
        const events = ['play', 'pause', 'ended', 'timeupdate', 'durationchange', 'loadedmetadata', 'volumechange', 'seeked'];
        events.forEach((name) => el.addEventListener(name, sync));
        sync();
        return () => events.forEach((name) => el.removeEventListener(name, sync));
    }, [video]);

    const toggle = () => {
        const el = video.current;
        if (!el) return;
        if (el.paused || el.ended) void el.play().catch(() => {});
        else el.pause();
    };

    return (
        <>
            {!playing && (
                <button
                    type="button"
                    className="pt-turned-video-play"
                    style={{ width: discSize, height: discSize }}
                    onClick={(e) => { e.stopPropagation(); toggle(); }}
                    aria-label="Play video"
                >
                    <PlayIcon aria-hidden="true" />
                </button>
            )}
            <div className="pt-turned-video-bar" role="group" aria-label="Video playback" onClick={(e) => e.stopPropagation()}>
                <button type="button" onClick={toggle} aria-label={playing ? 'Pause' : 'Play'}>
                    {playing ? <PauseIcon aria-hidden="true" /> : <PlayIcon aria-hidden="true" />}
                </button>
                <input
                    type="range"
                    min={0}
                    max={duration || 0}
                    step={0.1}
                    value={Math.min(time, duration || 0)}
                    disabled={!duration}
                    aria-label="Seek"
                    onChange={(e) => {
                        const el = video.current;
                        if (el) el.currentTime = Number(e.currentTarget.value);
                    }}
                />
                <span className="pt-turned-video-time">{clock(time)} / {clock(duration)}</span>
                <button
                    type="button"
                    onClick={() => { const el = video.current; if (el) el.muted = !el.muted; }}
                    aria-label={muted ? 'Unmute' : 'Mute'}
                    aria-pressed={muted}
                >
                    {muted ? <MutedIcon aria-hidden="true" /> : <VolumeIcon aria-hidden="true" />}
                </button>
            </div>
        </>
    );
};
