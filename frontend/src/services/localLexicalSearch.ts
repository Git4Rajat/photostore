/**
 * Client-side port of backend/search_utils.py's lexical scoring plus the
 * person/location/date matching helpers from backend/app.py, so most
 * /photos/search queries (tag, text, location, people, date -- everything
 * except real semantic/CLIP scoring, which stays server-side as Phase B) can
 * be answered entirely against the locally-cached lexical index without a
 * round trip. Kept as a line-for-line-faithful port on purpose: any future
 * change to the Python scoring must be mirrored here or the two will
 * silently rank results differently. See tests/localLexicalSearch.parity.test.ts,
 * which runs the same fixtures through both.
 */

export interface SearchTokens {
    subject: string[];
    location: string[];
    all: string[];
    expanded: string[];
    modifiers: string[];
    requiredObject: string[];
    exactPhrases: string[];
}

// --- Constants ported verbatim from search_utils.py ---

const TOKEN_CANONICAL_MAP: Record<string, string> = {
    automobile: 'car', automobiles: 'car', bike: 'bicycle', bikes: 'bicycle',
    building: 'architecture', buildings: 'architecture', campus: 'campus',
    canvas: 'canvas', child: 'child', children: 'child', circus: 'circus',
    colours: 'color', focus: 'focus', grey: 'gray',
    lens: 'lens', octopus: 'octopus', virus: 'virus', human: 'person',
    humans: 'person', leaves: 'leaf', men: 'man', sverige: 'sweden',
    malmo: 'malmo', 'malmö': 'malmo', nyc: 'new york', people: 'person',
    persons: 'person', pics: 'photo', pictures: 'photo', sea: 'ocean',
    seas: 'ocean', women: 'woman',
};

const VISUAL_MODIFIERS = new Set([
    'black', 'blue', 'brown', 'gold', 'gray', 'green', 'grey', 'orange',
    'pink', 'purple', 'red', 'silver', 'tan', 'teal', 'white', 'yellow',
    'big', 'small', 'large', 'tiny', 'huge', 'little', 'tall', 'short',
    'old', 'new', 'vintage', 'antique', 'young',
    'wooden', 'metal', 'glass', 'plastic', 'leather', 'stone',
]);

const SEARCH_STOP_WORDS = new Set([
    'in', 'at', 'near', 'from', 'by', 'with', 'wearing', 'holding', 'beside',
    'next', 'to', 'and', 'the', 'a', 'an',
]);

const MODIFIER_FILLER_WORDS = new Set(['color', 'colour']);

const SEMANTIC_TERM_EXPANSIONS: Record<string, string[]> = {
    water: ['waterfall', 'river', 'lake', 'ocean', 'beach', 'waterfront', 'seaside', 'pool', 'fountain', 'swimming', 'surfing', 'sailing', 'kayaking', 'rafting', 'diving', 'snorkeling', 'reef', 'harbor', 'dock', 'marina'],
    waterfall: ['water', 'river', 'stream', 'nature', 'landscape', 'mist'],
    ocean: ['sea', 'seaside', 'seascape', 'beach', 'water', 'waves', 'surfing', 'sailing', 'reef'],
    beach: ['sand', 'ocean', 'sea', 'seaside', 'water', 'surfing', 'summer'],
    river: ['water', 'waterfall', 'bridge', 'nature', 'landscape'],
    lake: ['water', 'mountain', 'forest', 'reflection', 'landscape'],
    sky: ['clouds', 'sunset', 'sunrise', 'rainbow', 'night sky', 'stars', 'twilight'],
    blue: ['sky', 'ocean', 'water', 'lake', 'river', 'pool', 'seascape', 'waterfront', 'night sky', 'ice'],
    green: ['grass', 'tree', 'forest', 'garden', 'park', 'field', 'leaf', 'nature'],
    white: ['snow', 'clouds', 'ice', 'wedding', 'winter'],
    yellow: ['sunrise', 'sunset', 'sunflower', 'flower', 'autumn'],
    orange: ['sunset', 'sunrise', 'autumn', 'fireworks'],
    red: ['rose', 'flower', 'sunset', 'festival', 'fireworks'],
    pink: ['flower', 'rose', 'orchid', 'sunset'],
    purple: ['flower', 'lavender', 'twilight', 'night sky'],
    black: ['night', 'shadow', 'silhouette', 'night sky'],
    grey: ['gray', 'clouds', 'fog', 'storm', 'rain'],
    gray: ['grey', 'clouds', 'fog', 'storm', 'rain'],
    nature: ['landscape', 'forest', 'tree', 'mountain', 'water', 'flower', 'wildlife'],
    landscape: ['nature', 'mountain', 'forest', 'field', 'valley', 'water', 'sky'],
    city: ['street', 'building', 'architecture', 'urban', 'cityscape', 'market'],
    food: ['meal', 'breakfast', 'lunch', 'dinner', 'dessert', 'restaurant', 'plate'],
};
const MAX_EXPANDED_TERMS_PER_TOKEN = 8;
const MAX_TAG_LENGTH = 48;
const MAX_TAGS_STORED = 40;

// --- Token normalization (search_utils._normalize_token / _singularize_word) ---

const singularizeWord = (word: string): string => {
    if (word.length <= 3 || word.endsWith('ss')) {
        return word;
    }
    if (word.endsWith('ies') && word.length > 4) {
        return `${word.slice(0, -3)}y`;
    }
    if (word.endsWith('ves') && word.length > 4) {
        return `${word.slice(0, -3)}f`;
    }
    if (word.endsWith('es') && word.length > 3) {
        const base = word.slice(0, -2);
        if (/(?:[sxz]|ch|sh|o)$/.test(base)) {
            return base;
        }
    }
    if (word.endsWith('s')) {
        return word.slice(0, -1);
    }
    return word;
};

// Strips combining diacritical marks (U+0300-U+036F) left behind by NFKD
// decomposition -- e.g. "caf\u00e9" -> "cafe" -- mirroring Python's
// unicodedata.normalize('NFKD', ...).encode('ascii', 'ignore').
const foldToAscii = (text: string): string => text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');

export const normalizeToken = (text: string): string => {
    const folded = foldToAscii(String(text ?? ''));
    const cleaned = folded.toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
    if (!cleaned) {
        return '';
    }
    const canonical: string[] = [];
    for (const tok of cleaned.split(' ')) {
        if (!tok) continue;
        const mapped = TOKEN_CANONICAL_MAP[tok] ?? tok;
        canonical.push(TOKEN_CANONICAL_MAP[mapped] ?? singularizeWord(mapped));
    }
    return canonical.join(' ');
};

export const normalizeSearchPhrase = (value: string): string => {
    const folded = foldToAscii(String(value ?? ''));
    return folded.toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
};

export const normalizeTags = (tags: string[]): string[] => {
    const normalized: string[] = [];
    const seen = new Set<string>();
    for (const tag of tags) {
        let cleaned = normalizeToken(String(tag));
        if (!cleaned || cleaned.includes(' ')) continue;
        if (cleaned.length > MAX_TAG_LENGTH) cleaned = cleaned.slice(0, MAX_TAG_LENGTH);
        if (!seen.has(cleaned)) {
            normalized.push(cleaned);
            seen.add(cleaned);
        }
        if (normalized.length >= MAX_TAGS_STORED) break;
    }
    return normalized;
};

const parseJsonList = (raw: unknown): string[] => {
    if (Array.isArray(raw)) return normalizeTags(raw.map((v) => String(v)));
    if (typeof raw === 'string') {
        try {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) return normalizeTags(parsed.map((v) => String(v)));
        } catch {
            // fall through
        }
    }
    return [];
};

const parseTags = (raw: unknown): string[] => parseJsonList(raw);

// --- Row -> searchable text (search_utils.py: location_tags/effective_tags/build_semantic_text) ---

export const locationTags = (row: Record<string, unknown>): string[] => normalizeTags([
    String(row.locationCity ?? ''),
    String(row.locationRegion ?? ''),
    String(row.locationCountry ?? ''),
    String(row.address ?? ''),
]);

const predictionTags = (row: Record<string, unknown>): string[] => {
    try {
        const processing = JSON.parse(String(row.processing_metadata ?? '{}') || '{}');
        const aiVision = processing?.client_ai_vision;
        const predictions = aiVision?.predictions;
        if (!Array.isArray(predictions)) return [];
        const labels: string[] = [];
        for (const item of predictions.slice(0, 160)) {
            if (!item || typeof item !== 'object') continue;
            const score = Number((item as any).score) || 0;
            if (score < 0.2) continue;
            labels.push(String((item as any).label ?? ''));
        }
        return normalizeTags(labels);
    } catch {
        return [];
    }
};

const gpsPresenceTags = (row: Record<string, unknown>, exifData: Record<string, string>): string[] => {
    const gpsPresent = 'GPSInfo' in exifData || Object.keys(exifData).some((k) => k.startsWith('GPS.'));
    const hasReadableLocation = Boolean(String(row.latitude ?? '').trim() && String(row.longitude ?? '').trim())
        || Boolean(String(row.locationCity ?? '').trim() || String(row.locationCountry ?? '').trim() || String(row.address ?? '').trim());
    if (gpsPresent && !hasReadableLocation) return ['gps tagged', 'location metadata'];
    return [];
};

const facePresenceTags = (row: Record<string, unknown>): string[] => {
    const faceCount = Number(row.faceCount) || 0;
    if (faceCount <= 0) return [];
    const tags: string[] = [];
    if (faceCount === 1) tags.push('selfie');
    else if (faceCount === 2) tags.push('selfie', 'pair');
    else tags.push('people', 'group', 'family', 'crowd');
    const aiPersonLabel = normalizeToken(String(row.aiPersonLabel ?? ''));
    if (aiPersonLabel) tags.push(aiPersonLabel);
    return normalizeTags(tags);
};

export const effectiveTags = (row: Record<string, unknown>, exifData: Record<string, string>): string[] => {
    const subjects = parseJsonList(row.subjectTags ?? '[]');
    const people = parseJsonList(row.peopleNames ?? '[]');
    const stored = parseTags(row.tags ?? '[]');
    const objects = parseJsonList(row.objects ?? '[]');
    const background = parseJsonList(row.backgroundTags ?? '[]');
    const prediction = predictionTags(row);
    const location = locationTags(row);
    const face = facePresenceTags(row);
    const gps = gpsPresenceTags(row, exifData);

    const primary = normalizeTags([...subjects, ...people, ...location]);
    const secondary = normalizeTags([...stored, ...objects, ...background, ...prediction, ...face, ...gps]);
    const primarySet = new Set(primary);
    return [...primary, ...secondary.filter((t) => !primarySet.has(t))];
};

export const buildSemanticText = (filename: string, row: Record<string, unknown>): string => {
    const subjectTags = parseJsonList(row.subjectTags ?? '[]');
    const location = locationTags(row);
    let semanticCandidates = new Set(normalizeTags([...subjectTags, ...location]));
    if (semanticCandidates.size === 0) {
        semanticCandidates = new Set(effectiveTags(row, {}));
    }

    const textParts: string[] = Array.from(semanticCandidates).sort();

    for (const field of [filename, row.caption, row.address, row.locationCity, row.locationRegion, row.locationCountry]) {
        const text = String(field ?? '').trim();
        if (!text) continue;
        for (const token of normalizeToken(text).split(' ')) {
            if (token && semanticCandidates.has(token)) textParts.push(token);
        }
    }

    const ocrText = String(row.ocrText ?? '').trim();
    if (ocrText) {
        for (const token of normalizeToken(ocrText).split(' ')) {
            if (token) textParts.push(token);
        }
    }

    return Array.from(new Set(textParts)).join(' ').slice(0, 10000);
};

// --- Query parsing (search_utils.parse_search_query / expand_search_terms) ---

const tokenVariants = (token: string): string[] => {
    if (!token) return [];
    const variants = [token];
    if (token.endsWith('ies') && token.length > 4) {
        variants.push(`${token.slice(0, -3)}y`);
    } else if (token.endsWith('es') && token.length > 3) {
        const base = token.slice(0, -2);
        if (/(?:[sxz]|ch|sh|o)$/.test(base)) {
            variants.push(base);
        } else {
            variants.push(token.slice(0, -1));
        }
    } else if (token.endsWith('s') && token.length > 3 && !token.endsWith('ss')) {
        variants.push(token.slice(0, -1));
    }

    const bases = [...variants];
    for (const base of bases) {
        if (base.endsWith('y') && base.length > 2) {
            variants.push(`${base.slice(0, -1)}ies`);
        } else if (/(?:[sxz]|ch|sh)$/.test(base)) {
            variants.push(`${base}es`);
        } else {
            variants.push(`${base}s`);
        }
    }
    return Array.from(new Set(variants));
};

const wordBoundaryRegexEscape = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const containsTerm = (text: string, token: string): boolean =>
    tokenVariants(token).some((variant) => new RegExp(`\\b${wordBoundaryRegexEscape(variant)}\\b`).test(text));

const containsRelatedTerm = (text: string, token: string): boolean => {
    if (!token) return false;
    if (containsTerm(text, token)) return true;
    if (token.length < 4) return false;
    return new RegExp(`\\b${wordBoundaryRegexEscape(token)}[a-z0-9]*\\b`).test(text);
};

export const expandSearchTerms = (tokens: string[]): string[] => {
    const expanded: string[] = [];
    const seen = new Set(tokens);
    for (const token of tokens) {
        for (const variant of tokenVariants(token)) {
            const normalizedVariant = normalizeToken(variant);
            if (normalizedVariant && !seen.has(normalizedVariant)) {
                expanded.push(normalizedVariant);
                seen.add(normalizedVariant);
            }
        }
        for (const related of (SEMANTIC_TERM_EXPANSIONS[token] ?? []).slice(0, MAX_EXPANDED_TERMS_PER_TOKEN)) {
            for (const part of normalizeToken(related).split(' ')) {
                if (part && !seen.has(part)) {
                    expanded.push(part);
                    seen.add(part);
                }
            }
        }
    }
    return expanded;
};

export const parseSearchQuery = (query: string): SearchTokens => {
    const clean = normalizeToken(query);
    if (!clean) {
        return { subject: [], location: [], all: [], expanded: [], modifiers: [], requiredObject: [], exactPhrases: [] };
    }

    const splitMatch = clean.match(/\b(?:in|at|near|from)\b/);
    let subjectTokens: string[];
    let locationTokens: string[] = [];
    if (splitMatch && splitMatch.index !== undefined) {
        const before = clean.slice(0, splitMatch.index).trim();
        const after = clean.slice(splitMatch.index + splitMatch[0].length).trim();
        subjectTokens = before.split(' ').filter((t) => t && !SEARCH_STOP_WORDS.has(t));
        const locSplitMatch = after.match(/\b(?:by|with|wearing|holding|beside|next to)\b/);
        const locationPart = locSplitMatch && locSplitMatch.index !== undefined ? after.slice(0, locSplitMatch.index).trim() : after;
        locationTokens = locationPart.split(' ').filter((t) => t && !SEARCH_STOP_WORDS.has(t));
    } else {
        subjectTokens = clean.split(' ').filter((t) => t && !SEARCH_STOP_WORDS.has(t));
    }

    if (subjectTokens.length === 0 && locationTokens.length > 0) {
        subjectTokens = [...locationTokens];
    }

    let allTokens = clean.split(' ').filter((t) => t && !SEARCH_STOP_WORDS.has(t));
    if (allTokens.length === 0) {
        allTokens = clean.split(' ').filter((t) => t);
    }

    let modifiers: string[] = [];
    let requiredObject: string[] = [];
    let exactPhrases: string[] = [];
    const searchableTerms = allTokens.filter((t) => !MODIFIER_FILLER_WORDS.has(t));

    if (allTokens.length >= 3 && MODIFIER_FILLER_WORDS.has(allTokens[1]) && VISUAL_MODIFIERS.has(allTokens[2])) {
        modifiers = [allTokens[2]];
        requiredObject = [allTokens[0]];
    } else if (searchableTerms.length >= 2) {
        for (let idx = 0; idx < searchableTerms.length - 1; idx += 1) {
            if (VISUAL_MODIFIERS.has(searchableTerms[idx]) && !VISUAL_MODIFIERS.has(searchableTerms[idx + 1])) {
                modifiers = [searchableTerms[idx]];
                requiredObject = [searchableTerms[idx + 1]];
                break;
            }
        }
        if (requiredObject.length === 0) {
            const foundModifiers = searchableTerms.slice(0, -1).filter((t) => VISUAL_MODIFIERS.has(t));
            const last = searchableTerms[searchableTerms.length - 1];
            if (foundModifiers.length > 0 && !VISUAL_MODIFIERS.has(last)) {
                modifiers = foundModifiers.slice(0, 1);
                requiredObject = [last];
            }
        }
    }

    if (modifiers.length > 0 && requiredObject.length > 0) {
        const modifier = modifiers[0];
        const obj = requiredObject[0];
        exactPhrases = [
            `${modifier} ${obj}`,
            `wearing ${modifier} ${obj}`,
            `${obj} color ${modifier}`,
            `${obj} colour ${modifier}`,
        ];
    }

    const expandedTerms = expandSearchTerms(allTokens);

    return {
        subject: subjectTokens,
        location: locationTokens,
        all: allTokens,
        expanded: expandedTerms,
        modifiers,
        requiredObject,
        exactPhrases,
    };
};

export const buildExpandedQueryText = (query: string, tokens?: SearchTokens): string => {
    const resolved = tokens ?? parseSearchQuery(query);
    const clean = normalizeToken(query);
    if (resolved.expanded.length === 0) return clean || query;
    return [clean, ...resolved.expanded].join(' ').trim();
};

// --- Lexical scoring (search_utils.lexical_search_score) ---

export const lexicalSearchScore = (
    tokens: SearchTokens,
    filename: string,
    row: Record<string, unknown>,
    exifData: Record<string, string>,
): number => {
    const tags = effectiveTags(row, exifData);
    const tagSet = new Set(tags);
    const filenameText = normalizeToken(filename);
    const semanticText = normalizeToken(buildSemanticText(filename, row));
    const subjectText = [filenameText, semanticText, normalizeToken(exifData.Model ?? '')].join(' ');
    const locationText = normalizeToken([
        String(row.address ?? ''), String(row.locationCity ?? ''),
        String(row.locationRegion ?? ''), String(row.locationCountry ?? ''),
    ].join(' '));
    const locationTextWithTags = normalizeToken([locationText, tags.join(' ')].join(' '));

    const { subject: subjectTokens, location: locationTokens, all: allTokens, expanded: expandedTokens, modifiers, requiredObject, exactPhrases } = tokens;

    if (allTokens.length === 0) return 0.0;

    if (locationTokens.length > 0 && !locationTokens.every((t) => containsRelatedTerm(locationTextWithTags, t))) {
        return 0.0;
    }

    let score = 0.0;
    if (requiredObject.length > 0) {
        const objectToken = requiredObject[0];
        const objectInTags = tokenVariants(objectToken).some((v) => tagSet.has(v));
        const objectInText = containsRelatedTerm(subjectText, objectToken);
        if (!objectInTags && !objectInText) return 0.0;
        score += objectInTags ? 6.0 : 3.5;
    }

    let modifierMatches = 0;
    for (const modifier of modifiers) {
        const modifierInTags = tagSet.has(modifier);
        const modifierInText = containsRelatedTerm(subjectText, modifier);
        if (modifierInTags || modifierInText) {
            modifierMatches += 1;
            score += modifierInTags ? 3.0 : 1.5;
        }
    }
    if (modifiers.length > 0 && modifierMatches === modifiers.length) {
        score += 12.0;
    }

    for (const phrase of exactPhrases) {
        const normalizedPhrase = normalizeToken(phrase);
        if (normalizedPhrase && subjectText.includes(normalizedPhrase)) {
            score += 5.0;
        }
    }

    for (const token of allTokens) {
        if (tagSet.has(token)) {
            score += 3.0;
        } else if (containsRelatedTerm(subjectText, token)) {
            score += 1.5;
        }
        if (containsRelatedTerm(semanticText, token)) score += 2.0;
        if (containsRelatedTerm(locationTextWithTags, token)) score += 2.5;
    }

    let expandedScore = 0.0;
    for (const token of expandedTokens) {
        if (tagSet.has(token)) {
            expandedScore += 1.0;
        } else if (containsRelatedTerm(subjectText, token)) {
            expandedScore += 0.7;
        }
        if (containsRelatedTerm(semanticText, token)) expandedScore += 0.8;
        if (containsRelatedTerm(locationTextWithTags, token)) expandedScore += 0.8;
    }
    score += Math.min(expandedScore, 5.0);

    if (subjectTokens.length > 0 && locationTokens.length > 0) {
        score += 3.0;
    }
    return score;
};

// --- People / location / date matching (app.py: _matched_query_people_groups, _matched_query_locations, _row_passes_search_filters) ---

export const matchedQueryPeopleGroups = (queryText: string, nameToIds: Record<string, string[]>): string[][] => {
    const queryNorm = normalizeSearchPhrase(queryText);
    const groups: string[][] = [];
    for (const [name, personIds] of Object.entries(nameToIds)) {
        if (name && new RegExp(`(^| )${wordBoundaryRegexEscape(name)}( |$)`).test(queryNorm)) {
            groups.push(Array.from(new Set(personIds)));
        }
    }
    return groups;
};

const knownLocationTerms = (rows: Record<string, unknown>[]): string[] => {
    const terms: string[] = [];
    const termSet = new Set<string>();
    for (const row of rows) {
        for (const field of ['locationCity', 'locationRegion', 'locationCountry', 'address']) {
            const term = normalizeSearchPhrase(String(row[field] ?? ''));
            for (const part of term.split(' ')) {
                if (part.length >= 3 && !termSet.has(part)) {
                    terms.push(part);
                    termSet.add(part);
                }
            }
            if (term && !termSet.has(term)) {
                terms.push(term);
                termSet.add(term);
            }
        }
    }
    return terms.sort((a, b) => b.length - a.length);
};

export const matchedQueryLocations = (queryText: string, rows: Record<string, unknown>[]): string[] => {
    const queryNorm = normalizeSearchPhrase(queryText);
    return knownLocationTerms(rows).filter((term) => new RegExp(`(^| )${wordBoundaryRegexEscape(term)}( |$)`).test(queryNorm));
};

const metadataMatchesLocations = (row: Record<string, unknown>, locationTerms: string[]): boolean => {
    if (locationTerms.length === 0) return true;
    const locationText = normalizeSearchPhrase([
        String(row.address ?? ''), String(row.locationCity ?? ''),
        String(row.locationRegion ?? ''), String(row.locationCountry ?? ''),
    ].join(' '));
    return locationTerms.some((term) => locationText.includes(term));
};

// --- Capture date (ordering_utils.metadata_capture_datetime + app._capture_in_range) ---

const parseIsoDate = (value: string): Date | null => {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
};

const parseCaptureDateFromExif = (exifData: Record<string, string>): Date | null => {
    const raw = (
        exifData.DateTimeOriginal || exifData.DateTime || exifData.CreationDate
        || exifData.CreateDate || exifData.MediaCreateDate || exifData.TrackCreateDate || ''
    ).trim();
    if (!raw) return null;
    // exiftool format: "YYYY:MM:DD HH:MM:SS" optionally with .ffffff and/or a
    // timezone offset -- reshape to an ISO string Date can parse natively.
    const m = raw.match(/^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/);
    if (!m) return null;
    const [, y, mo, d, h, mi, s, frac, tz] = m;
    const iso = `${y}-${mo}-${d}T${h}:${mi}:${s}${frac ?? ''}${tz ? tz.replace(/^([+-]\d{2})(\d{2})$/, '$1:$2') : 'Z'}`;
    return parseIsoDate(iso);
};

export const metadataUploadDatetime = (row: Record<string, unknown>): Date | null =>
    parseIsoDate(String(row.uploadDate || row.upload_started_at || row.last_processing_update || ''));

const metadataClientLastModifiedDatetime = (row: Record<string, unknown>): Date | null =>
    parseIsoDate(String(row.clientLastModified || ''));

export const metadataCaptureDatetime = (row: Record<string, unknown>, exifData: Record<string, string>): Date | null =>
    parseCaptureDateFromExif(exifData) || metadataClientLastModifiedDatetime(row) || metadataUploadDatetime(row);

const dayOnly = (d: Date): number => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());

export const captureInRange = (
    row: Record<string, unknown>,
    exifData: Record<string, string>,
    captureStart: Date | null,
    captureEnd: Date | null,
): boolean => {
    if (!captureStart && !captureEnd) return true;
    const captured = metadataCaptureDatetime(row, exifData);
    if (!captured) return false;
    const capturedDay = dayOnly(captured);
    if (captureStart && capturedDay < dayOnly(captureStart)) return false;
    if (captureEnd && capturedDay > dayOnly(captureEnd)) return false;
    return true;
};

export const rowPassesSearchFilters = (
    row: Record<string, unknown>,
    exifData: Record<string, string>,
    captureStart: Date | null,
    captureEnd: Date | null,
    matchedPersonGroups: string[][],
    matchedLocationTerms: string[],
): boolean => {
    if ((captureStart || captureEnd) && !captureInRange(row, exifData, captureStart, captureEnd)) {
        return false;
    }
    if (matchedPersonGroups.length > 0) {
        let peopleIds: Set<string>;
        try {
            peopleIds = new Set((JSON.parse(String(row.peopleIds ?? '[]') || '[]') as unknown[]).map((v) => String(v)));
        } catch {
            peopleIds = new Set();
        }
        const everyGroupMatched = matchedPersonGroups.every((group) => group.some((pid) => peopleIds.has(pid)));
        if (!everyGroupMatched) return false;
    }
    if (!metadataMatchesLocations(row, matchedLocationTerms)) return false;
    return true;
};

// --- Combined score (app._score_search_row, lexical-only: no query_embedding/vector_scores) ---

export const scoreSearchRowLexicalOnly = (
    tokens: SearchTokens,
    filename: string,
    row: Record<string, unknown>,
    exifData: Record<string, string>,
    matchedPersonGroups: string[][],
    matchedLocationTerms: string[],
): number => {
    let score = lexicalSearchScore(tokens, filename, row, exifData);
    if (matchedPersonGroups.length > 0) score += 8.0 * matchedPersonGroups.length;
    if (matchedLocationTerms.length > 0) score += 5.0;
    return score;
};

// --- Semantic blend (app._score_search_row's semantic half, app.cosine_similarity) ---
// Phase B: unlike the server (which precomputes a top-K vector_scores dict
// via vector_search_candidates), the client holds every row's real embedding
// locally after parsing the vector index, so it always compares directly
// rather than needing that shortlist-and-fallback dance -- equivalent to the
// server's "vector_scores contains every candidate" case, which is also the
// only case that matters once vector_search_candidates isn't dimension-
// mismatched into returning nothing (see get_vector_index_manifest_summary's
// docstring in storage_utils.py for why that mismatch happens server-side).
export const SEMANTIC_SEARCH_THRESHOLD_DEFAULT = 0.16;

// Mirrors search_utils.cosine_similarity exactly, including its min-length
// truncation (never hit in practice here since both sides are always the
// same real CLIP dimension) and zero-norm guard.
export const cosineSimilarity = (a: ArrayLike<number>, b: ArrayLike<number>): number => {
    const n = Math.min(a.length, b.length);
    if (n === 0) return 0;
    let dot = 0;
    let normA = 0;
    let normB = 0;
    for (let i = 0; i < n; i += 1) {
        dot += a[i] * b[i];
        normA += a[i] * a[i];
        normB += b[i] * b[i];
    }
    normA = Math.sqrt(normA);
    normB = Math.sqrt(normB);
    if (normA === 0 || normB === 0) return 0;
    return dot / (normA * normB);
};

export interface SemanticSearchContext {
    queryEmbedding: ArrayLike<number>;
    getEmbedding: (filename: string) => ArrayLike<number> | undefined;
    threshold?: number;
}

// app._score_search_row, full lexical+semantic blend.
export const scoreSearchRow = (
    tokens: SearchTokens,
    filename: string,
    row: Record<string, unknown>,
    exifData: Record<string, string>,
    matchedPersonGroups: string[][],
    matchedLocationTerms: string[],
    semantic?: SemanticSearchContext,
): number => {
    let score = lexicalSearchScore(tokens, filename, row, exifData);
    if (semantic) {
        const rowEmbedding = semantic.getEmbedding(filename);
        if (rowEmbedding) {
            const semanticScore = cosineSimilarity(semantic.queryEmbedding, rowEmbedding);
            if (semanticScore >= (semantic.threshold ?? SEMANTIC_SEARCH_THRESHOLD_DEFAULT)) {
                score += semanticScore * 10.0;
            }
        }
    }
    if (matchedPersonGroups.length > 0) score += 8.0 * matchedPersonGroups.length;
    if (matchedLocationTerms.length > 0) score += 5.0;
    return score;
};

// --- Whole-query orchestration, mirroring routes/photos.py's search_photos() ---

export interface LocalSearchablePeopleIndex {
    pidToName: Record<string, string>;
    nameToIds: Record<string, string[]>;
}

export interface LocalSearchPage {
    filenames: string[];
    total: number;
}

export const runLocalSearch = (
    rows: Record<string, unknown>[],
    peopleNameIndex: LocalSearchablePeopleIndex,
    query: string,
    offset: number,
    limit: number,
    captureStart: Date | null,
    captureEnd: Date | null,
    semantic?: SemanticSearchContext,
): LocalSearchPage => {
    const tokens = parseSearchQuery(query);
    const matchedPersonGroups = matchedQueryPeopleGroups(query, peopleNameIndex.nameToIds);
    const matchedLocationTerms = matchedQueryLocations(query, rows);

    const scored: { score: number; filename: string }[] = [];
    for (const row of rows) {
        const filename = String(row.RowKey ?? '');
        if (!filename) continue;
        let exifData: Record<string, string> = {};
        try {
            exifData = JSON.parse(String(row.exifData ?? '{}') || '{}');
        } catch {
            exifData = {};
        }
        if (!rowPassesSearchFilters(row, exifData, captureStart, captureEnd, matchedPersonGroups, matchedLocationTerms)) {
            continue;
        }
        const score = scoreSearchRow(tokens, filename, row, exifData, matchedPersonGroups, matchedLocationTerms, semantic);
        if (score <= 0) continue;
        scored.push({ score, filename });
    }
    scored.sort((a, b) => b.score - a.score);
    const total = scored.length;
    const filenames = scored.slice(offset, offset + limit).map((s) => s.filename);
    return { filenames, total };
};
