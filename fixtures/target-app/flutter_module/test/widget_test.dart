import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pulse_target_flutter/main.dart';

void main() {
  test('parses faults from entrypoint args', () {
    final f = FlutterFaults.fromArgs(['flutter_amount_wrong, x', '']);
    expect(f.amountWrong, isTrue);
    expect(f.buttonMissing, isFalse);
  });

  test('wallet redeem and displayed balance', () {
    final w = Wallet(1000);
    expect(w.redeem(rewards[1]), isTrue);
    expect(w.balance, 550);
    expect(w.redeem(rewards[2]), isFalse);
    expect(w.displayed(const FlutterFaults({'flutter_amount_wrong'})), 551);
  });

  testWidgets('redeem flow', (tester) async {
    await tester.pumpWidget(const TargetFlutterApp(faults: FlutterFaults({})));
    expect(find.text('可用积分：1000'), findsOneWidget);
    await tester.tap(find.byKey(const Key('btn_redeem')));
    await tester.pump();
    expect(find.text('请选择奖励'), findsOneWidget);
    await tester.tap(find.byKey(const Key('reward_0')));
    await tester.tap(find.byKey(const Key('btn_redeem')));
    await tester.pump();
    expect(find.text('兑换成功：中杯升大杯券'), findsOneWidget);
    expect(find.text('可用积分：880'), findsOneWidget);
  });

  testWidgets('button missing fault', (tester) async {
    await tester.pumpWidget(
      const TargetFlutterApp(faults: FlutterFaults({'flutter_button_missing'})),
    );
    expect(find.byKey(const Key('btn_redeem')), findsNothing);
  });
}
