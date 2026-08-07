// Generates src/mocks/fixtures/generated.ts from the REAL fixtures produced by
// 07-28-skill-contract (shared/tests/fixtures/*.json).
//
// Why generate instead of `import x from '...json'`: a JSON import widens
// `"listening_material"` to string and `[]` to never[], so it can never be
// checked against the contract types. Emitting an annotated TS literal
// (`export const M: Material = {...}`) makes tsc verify the real fixture data
// against the generated schema types — which is the first line of defence
// implement.md phase 2 asks for.
//
//   node scripts/gen-fixtures.mjs           # write
//   node scripts/gen-fixtures.mjs --check   # fail if output would change

import { execFileSync } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const fxDir = resolve(repoRoot, 'skills/shared/tests/fixtures')
const outPath = resolve(here, '..', 'src/mocks/fixtures/generated.ts')

// BLUEPRINT_V1_LEGACY earns its place twice over. It is the frontend's only REAL v1 record — the
// alternative, hand-downgrading the v2 fixture inside a test, can only ever contain the fields
// whoever wrote the test remembered to change — and its `: Blueprint` annotation makes tsc assert
// that an archived record still satisfies the contract the frontend generates from the READ schema.
// If someone points codegen back at the write-side schema, this line stops compiling: `item_form:
// "multiple_choice"` and `form_group: null` are both in there.
const FILES = [
  { file: 'material_valid.json', name: 'MATERIAL_VALID', type: 'Material' },
  { file: 'blueprint_valid.json', name: 'BLUEPRINT_VALID', type: 'Blueprint' },
  { file: 'blueprint_v1_legacy.json', name: 'BLUEPRINT_V1_LEGACY', type: 'Blueprint' },
  { file: 'blueprint_bad_anchor.json', name: 'BLUEPRINT_BAD_ANCHOR', type: 'Blueprint' },
  { file: 'audit_valid.json', name: 'AUDIT_VALID', type: 'Audit' },
  { file: 'audit_aligned.json', name: 'AUDIT_ALIGNED', type: 'Audit' },
]

const chunks = [
  `/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/shared/tests/fixtures/
 * Regenerate: npm run fixtures:gen
 *
 * These are the real fixtures from 07-28-skill-contract, not invented data.
 * The explicit type annotations make tsc verify them against the frozen
 * schema-generated contract types.
 */
import type { Audit, Blueprint, Material, QuestionPackage } from '@/contracts'
`,
]

for (const t of FILES) {
  const data = JSON.parse(await readFile(resolve(fxDir, t.file), 'utf8'))
  chunks.push(`\nexport const ${t.name}: ${t.type} = ${JSON.stringify(data, null, 2)}\n`)
}

// The question package is not a committed fixture and deliberately is not one: `build_fixtures.py`
// owns `fixtures/` and the package is assembled in memory from material_valid + blueprint_valid by
// the 60-check suite's own helper (`run_tests._question_package`), which is also what
// backend/tests/conftest.py imports. So it is generated the same way rather than copied: a
// hand-written face here would be the frontend's own idea of the contract, and the one thing it
// would be free to get wrong is the three-block separation this whole tab is built around.
//
// The mixed form/note/table group structure matters as much as the fields: it is what the tab's
// three real layouts are checked against, and inventing it would mean inventing the shape of the
// thing under test.
const questionPackage = JSON.parse(
  execFileSync(
    'python3',
    [
      '-c',
      [
        'import json, sys',
        `sys.path.insert(0, ${JSON.stringify(resolve(repoRoot, 'skills/shared/tests'))})`,
        'import run_tests',
        'print(json.dumps(run_tests._question_package(), ensure_ascii=False))',
      ].join('\n'),
    ],
    { cwd: repoRoot, encoding: 'utf8' },
  ),
)
chunks.push(
  `\nexport const QUESTION_PACKAGE_VALID: QuestionPackage = ${JSON.stringify(questionPackage, null, 2)}\n`,
)

const next = chunks.join('')

if (process.argv.includes('--check')) {
  let current = ''
  try {
    current = await readFile(outPath, 'utf8')
  } catch {
    /* missing counts as drift */
  }
  if (current !== next) {
    console.error('fixtures drift: src/mocks/fixtures/generated.ts')
    console.error('Run `npm run fixtures:gen` and commit the result.')
    process.exit(1)
  }
  console.log('fixtures up to date')
} else {
  await writeFile(outPath, next, 'utf8')
  console.log(`wrote src/mocks/fixtures/generated.ts (${FILES.length} fixtures)`)
}
