import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import { violationFingerprints } from '../accessibility'

const storybookURL: string = process.env.STORYBOOK_URL || 'https:storybook.posthog.net'

test(`lemon popup`, async ({ page }) => {
    await page.goto(storybookURL)
    await page.locator('[data-item-id="lemon-ui-popup--popup"]').click()

    await expect(page).toHaveScreenshot({ maxDiffPixels: 100, fullPage: true })
})

test(`lemon popup should only have allow-listed automatically detectable accessibility issues`, async ({ page }) => {
    await page.goto(storybookURL)
    await page.locator('[data-item-id="lemon-ui-popup--popup"]').click()

    const accessibilityScanResults = await new AxeBuilder({ page }).exclude('#bottom-notice').analyze()

    expect(violationFingerprints(accessibilityScanResults)).toMatchSnapshot()
})
