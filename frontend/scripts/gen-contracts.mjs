// Generates src/contracts/{material,blueprint,audit}.ts from the frozen JSON
// Schemas in skills/ielts-listening-skills/shared/schemas/.
//
// Single source of truth (design.md §10): the frontend never hand-writes the
// three contract types, so "the blueprint the frontend believes in" cannot
// diverge from "the blueprint the backend emits".
//
//   node scripts/gen-contracts.mjs            # write
//   node scripts/gen-contracts.mjs --check    # fail if output would change

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from 'json-schema-to-typescript'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const schemaDir = resolve(repoRoot, 'skills/ielts-listening-skills/shared/schemas')
const outDir = resolve(here, '..', 'src/contracts')

const TARGETS = [
  { schema: 'material.schema.json', out: 'material.ts', root: 'Material' },
  { schema: 'blueprint.schema.json', out: 'blueprint.ts', root: 'Blueprint' },
  { schema: 'audit.schema.json', out: 'audit.ts', root: 'Audit' },
]

const BANNER = [
  '/* eslint-disable */',
  '/**',
  ' * AUTO-GENERATED, DO NOT EDIT.',
  ' *',
  ' * Source: skills/ielts-listening-skills/shared/schemas/%SCHEMA%',
  ' * Regenerate: npm run contracts:gen',
  ' */',
  '',
].join('\n')

async function render(target) {
  const schemaPath = resolve(schemaDir, target.schema)
  const schema = JSON.parse(await readFile(schemaPath, 'utf8'))
  const body = await compile(schema, target.root, {
    bannerComment: '',
    additionalProperties: false,
    style: { semi: false, singleQuote: true },
  })
  return BANNER.replace('%SCHEMA%', target.schema) + body
}

const check = process.argv.includes('--check')
let drift = false

await mkdir(outDir, { recursive: true })
for (const target of TARGETS) {
  const next = await render(target)
  const outPath = resolve(outDir, target.out)
  if (check) {
    let current = ''
    try {
      current = await readFile(outPath, 'utf8')
    } catch {
      /* missing counts as drift */
    }
    if (current !== next) {
      drift = true
      console.error(`contracts drift: src/contracts/${target.out}`)
    }
  } else {
    await writeFile(outPath, next, 'utf8')
    console.log(`wrote src/contracts/${target.out}`)
  }
}

if (check && drift) {
  console.error('Run `npm run contracts:gen` and commit the result.')
  process.exit(1)
}
if (check) console.log('contracts up to date')
