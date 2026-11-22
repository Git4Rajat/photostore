/** Swipe up from the bottom of the screen to open the info panel, like iOS Photos. */

/** The very bottom strip is iOS's home-indicator swipe; a touch starting there is the system's, not ours. */
export const SYSTEM_EDGE_PX = 24;
/** A swipe has to start within this distance of the bottom (the action bar and the strip of photo above it). */
export const START_ZONE_PX = 140;
/** Upward travel that opens the panel. */
export const OPEN_DISTANCE_PX = 60;
/** A shorter flick still opens it when it is this fast (px per ms). */
export const OPEN_VELOCITY = 0.5;
export const FLICK_MIN_PX = 30;

/** Does a touch at `y` begin a bottom swipe, given the viewport's height? */
export const startsInBottomZone = (y: number, viewportHeight: number): boolean => {
    const fromBottom = viewportHeight - y;
    return fromBottom > SYSTEM_EDGE_PX && fromBottom <= START_ZONE_PX;
};

/** Is a finished one-finger drag (dx, dy in px, dt in ms) an upward swipe that should open the panel? */
export const isOpenSwipe = (dx: number, dy: number, dt: number): boolean => {
    const up = -dy;
    if (up <= 0 || up < Math.abs(dx) * 1.5) return false;
    if (up >= OPEN_DISTANCE_PX) return true;
    return up >= FLICK_MIN_PX && dt > 0 && up / dt >= OPEN_VELOCITY;
};
