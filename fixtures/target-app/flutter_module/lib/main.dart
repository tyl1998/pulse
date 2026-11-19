import 'package:flutter/material.dart';

/// Faults passed from the host app as Dart entrypoint args (comma separated keys).
class FlutterFaults {
  const FlutterFaults(this.keys);

  factory FlutterFaults.fromArgs(List<String> args) => FlutterFaults(
    args
        .expand((a) => a.split(','))
        .map((s) => s.trim())
        .where((s) => s.isNotEmpty)
        .toSet(),
  );

  final Set<String> keys;

  bool get amountWrong => keys.contains('flutter_amount_wrong');
  bool get buttonMissing => keys.contains('flutter_button_missing');
}

class Reward {
  const Reward(this.name, this.points);
  final String name;
  final int points;
}

const rewards = [
  Reward('中杯升大杯券', 120),
  Reward('任意饮品券', 450),
  Reward('周边马克杯', 900),
];

/// Pure balance logic so it can be unit-tested.
class Wallet {
  Wallet(this.balance);
  int balance;

  bool redeem(Reward r) {
    if (r.points > balance) return false;
    balance -= r.points;
    return true;
  }

  int displayed(FlutterFaults f) => f.amountWrong ? balance + 1 : balance;
}

void main(List<String> args) =>
    runApp(TargetFlutterApp(faults: FlutterFaults.fromArgs(args)));

class TargetFlutterApp extends StatelessWidget {
  const TargetFlutterApp({super.key, required this.faults});
  final FlutterFaults faults;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Flutter 积分兑换',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: Colors.green),
      home: RewardsPage(faults: faults),
    );
  }
}

class RewardsPage extends StatefulWidget {
  const RewardsPage({super.key, required this.faults});
  final FlutterFaults faults;

  @override
  State<RewardsPage> createState() => _RewardsPageState();
}

class _RewardsPageState extends State<RewardsPage> {
  final wallet = Wallet(1000);
  int? selected;
  String result = '未兑换';

  void _redeem() {
    final idx = selected;
    if (idx == null) {
      setState(() => result = '请选择奖励');
      return;
    }
    final r = rewards[idx];
    setState(() => result = wallet.redeem(r) ? '兑换成功：${r.name}' : '积分不足');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Flutter 积分兑换')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '可用积分：${wallet.displayed(widget.faults)}',
                key: const Key('points_balance'),
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 12),
              RadioGroup<int>(
                groupValue: selected,
                onChanged: (v) => setState(() => selected = v),
                child: Column(
                  children: [
                    for (var i = 0; i < rewards.length; i++)
                      RadioListTile<int>(
                        key: Key('reward_$i'),
                        value: i,
                        title: Text(rewards[i].name),
                        subtitle: Text('${rewards[i].points} 积分'),
                      ),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              if (!widget.faults.buttonMissing)
                FilledButton(
                  key: const Key('btn_redeem'),
                  onPressed: _redeem,
                  child: const Text('立即兑换'),
                ),
              const SizedBox(height: 12),
              Text(result, key: const Key('redeem_result')),
            ],
          ),
        ),
      ),
    );
  }
}
