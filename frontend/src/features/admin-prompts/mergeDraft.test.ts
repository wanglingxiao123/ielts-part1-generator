import { expect, it } from 'vitest'
import { mergeDraft } from './mergeDraft'

it('merges separate modules and identical concurrent edits', () => {
  expect(mergeDraft({ a: 'old', b: 'old' }, { a: 'mine', b: 'old' }, { a: 'old', b: 'theirs' })).toEqual({ values: { a: 'mine', b: 'theirs' }, baseline: { a: 'old', b: 'theirs' }, conflicts: [] })
  expect(mergeDraft({ a: 'old' }, { a: 'same' }, { a: 'same' }).conflicts).toEqual([])
})
it('keeps conflicts unresolved across repeated refreshes and detects a third edit', () => {
  const first = mergeDraft({ a: 'old', b: 'old' }, { a: 'mine', b: 'old' }, { a: 'theirs', b: 'new' })
  expect(first.conflicts).toEqual(['a'])
  expect(mergeDraft(first.baseline, first.values, { a: 'theirs', b: 'new' }).conflicts).toEqual(['a'])
  expect(mergeDraft({ a: 'theirs' }, { a: 'mine' }, { a: 'third' }).conflicts).toEqual(['a'])
})
it('requires choices for legacy recovery with no baseline and preserves empty edits', () => {
  expect(mergeDraft({}, { a: 'mine' }, { a: 'theirs' }).conflicts).toEqual(['a'])
  expect(mergeDraft({ a: 'old' }, { a: '' }, { a: 'old' }).values).toEqual({ a: '' })
})
