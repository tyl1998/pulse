"""Scripted target-app backing ``fixtures/golden-cases/p1-exit``.

The world is a small state machine of the real target app pages (home, list, cart, form,
dialog, delayed) written from the Kotlin sources: tapping a button changes the page, the cart
plus button increments a quantity, the form submit builds the result string, and the back key
returns to home. Node names, texts and states come from the app's stable resource ids.

This is a test double for the interpreter. It proves the ten exit flows are expressible and
self-consistent offline; it is never evidence about a physical phone.
"""

# ruff: noqa: RUF001 - the target app's strings use full-width CJK punctuation.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_schema import models as m

APP = "com.pulse.target"
CART_UNIT = {1: 32.00, 2: 28.50, 3: 18.00}
CART_NAME = {1: "拿铁", 2: "美式", 3: "可颂"}
HOME = "home"
VARIANTS = frozenset({"list", "cart", "form", "dialog", "delayed"})


@dataclass
class ExitWorld:
    """Mutable page state of the simulated app."""

    page: str = HOME
    cart_qty: dict[int, int] = field(default_factory=lambda: {1: 1, 2: 2, 3: 1})
    name: str = ""
    large: bool = False
    agree: bool = False
    submitted: str | None = None
    checkout: str | None = None
    dialog_tapped: bool = False
    alert_open: bool = False
    delayed_loaded: bool = False
    home_visits: int = 1


def _money(value: float) -> str:
    return f"¥{value:.2f}"


class ExitDriver:
    """Resolves only locator names that exist on the current simulated page."""

    app_id = APP

    def __init__(self, world: ExitWorld | None = None) -> None:
        self.world = world or ExitWorld()
        self.lease: RunLease | None = None
        self.actions: list[str] = []
        self.finished: list[RunStatus] = []

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        self.lease = RunLease(run_id, 1)
        return self.lease

    def _go(self, page: str) -> None:
        self.world.page = page
        if page == HOME:
            self.world.home_visits += 1

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        name = target.locator_ref if isinstance(target, m.LocatorRef) else "inline"
        node = self.node(name)
        if node is None:
            return Resolution("not_found", "no_match")
        return Resolution(
            "resolved",
            None,
            (node,),
            {"node_path": [0, 1]},
            candidate_index=0,
            candidate_by="res_id",
        )

    def node(self, name: str) -> Element | None:
        world = self.world
        page = world.page

        def element(text: str | None, **states: bool) -> Element:
            return Element(
                "tree",
                text,
                states.get("visible", True),
                {"enabled": True, "visible": True, "checked": None} | states,
                {"left": 10, "top": 200, "right": 300, "bottom": 280},
            )

        if page == HOME:
            return {
                "home-title": element("Pulse 靶场 v1"),
                "fault-banner": element("已开启错误注入：无"),
                "open-list": element("原生列表"),
                "open-form": element("表单"),
                "open-cart": element("购物车"),
                "open-dialog": element("弹窗"),
                "open-delayed": element("延迟加载"),
            }.get(name)
        if page == "list":
            return {
                "list-count": element("共 60 项"),
                "list-selected": element("未选择"),
            }.get(name)
        if page == "cart":
            if name == "cart-title":
                return element("购物车")
            if name.startswith("cart-name-"):
                index = int(name[-1])
                return element(CART_NAME[index])
            if name.startswith("cart-qty-"):
                index = int(name[-1])
                return element(str(world.cart_qty[index]))
            if name == "cart-total":
                total = sum(CART_UNIT[i] * qty for i, qty in world.cart_qty.items())
                return element(f"合计：{_money(total)}")
            if name.startswith("cart-plus-"):
                return element("+")
            if name.startswith("cart-minus-"):
                return element("-")
            if name == "checkout":
                return element("去结算")
            if name == "checkout-result":
                return element(world.checkout) if world.checkout is not None else None
            return None
        if page == "form":
            return {
                "form-title": element("用户信息"),
                "name": element(world.name),
                "large": element("大杯", checked=world.large),
                "agree": element("同意协议", checked=world.agree),
                "notify": element("接收通知", checked=False),
                "submit": element("提交", enabled=world.agree),
                "form-result": element(world.submitted) if world.submitted is not None else None,
            }.get(name)
        if page == "dialog":
            if name == "dialog-result":
                # The page shows 未操作 before the delete alert is confirmed, 已删除 after it.
                return element("已删除" if world.dialog_tapped else "未操作")
            if name == "show-alert":
                return element("删除地址")
            if name in {"dialog-cancel", "confirm-dialog-cancel"}:
                # Alert dialog buttons are only present while the alert is open.
                return element("取消") if world.alert_open else None
            return None
        if page == "delayed":
            if name == "delayed-hint":
                return element("加载中（2000ms）…", visible=not world.delayed_loaded)
            if name == "delayed-content":
                # Simulated load completes on the first read after the page opens.
                world.delayed_loaded = True
                return element("加载完成", visible=True)
            if name == "delayed-action":
                return element("继续", visible=world.delayed_loaded)
            return None
        return None

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        assert self.lease == lease
        world = self.world
        self.actions.append(plan.kind)
        if plan.kind == "key":
            if world.page != HOME:
                self._go(HOME)
            return ActionResult("completed", None, "action", "observation")
        if plan.kind == "set_text":
            world.name = str(plan.params.get("text", ""))
            return ActionResult("completed", None, "action", "observation")
        if plan.kind == "tap":
            assert plan.target is not None and plan.target.action_target is not None
            name = plan.target_ref or ""
            self._tap(name)
            return ActionResult("completed", None, "action", "observation")
        return ActionResult("rejected", "unsupported_action")

    def _tap(self, name: str) -> None:
        world = self.world
        page = world.page
        if page == HOME:
            targets = {
                "open-list": "list",
                "open-cart": "cart",
                "open-form": "form",
                "open-dialog": "dialog",
                "open-delayed": "delayed",
            }
            if name in targets:
                self._go(targets[name])
            return
        if page == "cart":
            if name.startswith("cart-plus-"):
                index = int(name[-1])
                world.cart_qty[index] += 1
            elif name == "checkout":
                total = sum(CART_UNIT[i] * qty for i, qty in world.cart_qty.items())
                world.checkout = f"下单成功，实付 {_money(total)}"
            return
        if page == "form":
            if name == "large":
                world.large = True
            elif name == "agree":
                world.agree = True
            elif name == "submit" and world.agree:
                size = "大杯" if world.large else "小杯"
                world.submitted = f"提交成功：{world.name} / {size} / 通知关"
            return
        if page == "dialog":
            if name == "show-alert":
                world.alert_open = True
            elif name in {"dialog-cancel", "confirm-dialog-cancel"}:
                world.alert_open = False
            return
        if page == "delayed" and name == "delayed-content":
            world.delayed_loaded = True

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        self.finished.append(state)
        return {"status": "cleaned", "state": state}
