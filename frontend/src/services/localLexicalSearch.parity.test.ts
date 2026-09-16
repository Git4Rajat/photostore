import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import {
    parseSearchQuery,
    lexicalSearchScore,
    scoreSearchRowLexicalOnly,
    scoreSearchRow,
    cosineSimilarity,
    matchedQueryPeopleGroups,
    matchedQueryLocations,
    rowPassesSearchFilters,
} from './localLexicalSearch';

// Shared golden fixture with backend/tests/test_lexical_search_parity_fixture.py --
// generated from the REAL search_utils.py/app.py functions (see
// backend/scripts/generate_lexical_search_parity_fixtures.py). If this test
// fails after a legitimate scoring change on the Python side, update this
// file's port (localLexicalSearch.ts) to match, then regenerate the fixture
// and confirm the Python-side test also still passes.
const FIXTURE_PATH = path.resolve(__dirname, '../../../backend/tests/fixtures/lexical_search_parity.json');

interface FixtureCase {
    name: string;
    filename: string;
    row: Record<string, unknown>;
    query: string;
    nameToIds: Record<string, string[]>;
    captureStart: string | null;
    captureEnd: string | null;
    expected: {
        tokens: {
            subject: string[];
            location: string[];
            all: string[];
            expanded: string[];
            modifiers: string[];
            requiredObject: string[];
            exactPhrases: string[];
        };
        matchedPersonGroups: string[][];
        matchedLocationTerms: string[];
        passesFilters: boolean;
        lexicalScore: number;
        combinedScore: number;
        semanticScore: number | null;
        combinedScoreWithSemantic: number | null;
    };
    queryEmbedding: number[] | null;
    rowEmbedding: number[] | null;
}

const cases: FixtureCase[] = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf-8'));

const parseDate = (value: string | null): Date | null => (value ? new Date(`${value}T00:00:00Z`) : null);

describe('localLexicalSearch parity with search_utils.py', () => {
    it('loads a non-trivial fixture', () => {
        expect(cases.length).toBeGreaterThanOrEqual(10);
    });

    it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, testCase) => {
        const tokens = parseSearchQuery(testCase.query);
        expect(tokens.subject).toEqual(testCase.expected.tokens.subject);
        expect(tokens.location).toEqual(testCase.expected.tokens.location);
        expect(tokens.all).toEqual(testCase.expected.tokens.all);
        expect(tokens.expanded).toEqual(testCase.expected.tokens.expanded);
        expect(tokens.modifiers).toEqual(testCase.expected.tokens.modifiers);
        expect(tokens.requiredObject).toEqual(testCase.expected.tokens.requiredObject);
        expect(tokens.exactPhrases).toEqual(testCase.expected.tokens.exactPhrases);

        const exifData: Record<string, string> = JSON.parse(String(testCase.row.exifData ?? '{}') || '{}');
        const matchedPersonGroups = matchedQueryPeopleGroups(testCase.query, testCase.nameToIds);
        const matchedLocationTerms = matchedQueryLocations(testCase.query, [testCase.row]);
        expect(matchedPersonGroups).toEqual(testCase.expected.matchedPersonGroups);
        expect(matchedLocationTerms).toEqual(testCase.expected.matchedLocationTerms);

        const captureStart = parseDate(testCase.captureStart);
        const captureEnd = parseDate(testCase.captureEnd);
        const passesFilters = rowPassesSearchFilters(testCase.row, exifData, captureStart, captureEnd, matchedPersonGroups, matchedLocationTerms);
        expect(passesFilters).toBe(testCase.expected.passesFilters);

        const lexicalScore = lexicalSearchScore(tokens, testCase.filename, testCase.row, exifData);
        expect(lexicalScore).toBeCloseTo(testCase.expected.lexicalScore, 5);

        const combinedScore = scoreSearchRowLexicalOnly(tokens, testCase.filename, testCase.row, exifData, matchedPersonGroups, matchedLocationTerms);
        expect(combinedScore).toBeCloseTo(testCase.expected.combinedScore, 5);

        if (testCase.queryEmbedding && testCase.rowEmbedding) {
            const semanticScore = cosineSimilarity(testCase.queryEmbedding, testCase.rowEmbedding);
            expect(semanticScore).toBeCloseTo(testCase.expected.semanticScore as number, 5);

            const combinedWithSemantic = scoreSearchRow(
                tokens, testCase.filename, testCase.row, exifData, matchedPersonGroups, matchedLocationTerms,
                { queryEmbedding: testCase.queryEmbedding, getEmbedding: () => testCase.rowEmbedding as number[] },
            );
            expect(combinedWithSemantic).toBeCloseTo(testCase.expected.combinedScoreWithSemantic as number, 5);
        }
    });
});
