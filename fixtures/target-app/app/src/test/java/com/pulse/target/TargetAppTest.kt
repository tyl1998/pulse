package com.pulse.target

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigDecimal

class CartTest {
    private val none = emptySet<Fault>()

    @Test fun totalWithoutFaults() {
        val cart = Cart.default()
        assertEquals(BigDecimal("107.00"), cart.displayedTotal(none))
        assertEquals("¥107.00", Cart.money(cart.displayedTotal(none)))
    }

    @Test fun incDecAndFloorAtZero() {
        val cart = Cart.default()
        cart.inc(0)
        assertEquals(BigDecimal("139.00"), cart.total())
        repeat(5) { cart.dec(2) }
        assertEquals(0, cart.items[2].qty)
    }

    @Test fun totalWrongAddsOneCent() {
        assertEquals(BigDecimal("107.01"), Cart.default().displayedTotal(setOf(Fault.CART_TOTAL_WRONG)))
    }

    @Test fun qtyOffByOneOnlyAffectsDisplay() {
        val cart = Cart.default()
        assertEquals(3, cart.displayedQty(1, setOf(Fault.CART_QTY_OFF_BY_ONE)))
        assertEquals(BigDecimal("107.00"), cart.total())
    }

    @Test fun itemMissingHidesSecondRowButKeepsTotal() {
        val cart = Cart.default()
        val faults = setOf(Fault.CART_ITEM_MISSING)
        assertEquals(listOf(0, 2), cart.visibleIndexes(faults))
        assertEquals(BigDecimal("107.00"), cart.displayedTotal(faults))
    }
}

class FaultRequestTest {
    @Test fun parsesKnownAndUnknown() {
        val req = FaultRequest.parse(false, " cart_total_wrong, nope ,popup_on_launch", 500)
        assertEquals(setOf(Fault.CART_TOTAL_WRONG, Fault.POPUP_ON_LAUNCH), req.faults)
        assertEquals(listOf("nope"), req.unknown)
        assertEquals(500, req.delayMs)
    }

    @Test fun absentFaultsMeansNoChange() {
        val req = FaultRequest.parse(true, null, -1)
        assertTrue(req.clear)
        assertNull(req.faults)
        assertNull(req.delayMs)
    }

    @Test fun emptyStringClearsSet() {
        assertEquals(emptySet<Fault>(), FaultRequest.parse(false, "", null).faults)
    }
}

class ListItemsTest {
    @Test fun skipsMissingItem() {
        val items = ListActivity.listItems(60, skip = 30)
        assertEquals(59, items.size)
        assertTrue("商品 30" !in items)
    }
}
