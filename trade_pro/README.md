
조건,sell_pro 상태,SELL 신호 발생 여부,이유
auto_sell = True,True (Pro ON),✅ 발생 가능,추세 전환 또는 Custom Rule이 만족되면 신호 발생.
auto_sell = True,False (Pro OFF),❌ 발생 안 함,"logger.debug로 명시된 대로 ""Periodic SELL suppressed""되어 신호가 차단됨."
auto_sell = False,True 또는 False,❌ 발생 안 함,최상위 스위치 if self.custom.auto_sell:에서 진입 자체가 차단됨.

