# Итерация A: простая заглушка модерации (позже: словари + скоринг)


BLACKLIST = {"порно", "насилие"}
GREYLIST = {"эротика", "жестокость"}


class ModResult:
    def __init__(self, allow: bool, soft: bool = False, reason: str = ""):
        self.allow = allow
        self.soft = soft
        self.reason = reason




def check_text(prompt: str) -> ModResult:
    p = prompt.lower()
    if any(word in p for word in BLACKLIST):
        return ModResult(False, False, "hard-block: blacklist match")
    if any(word in p for word in GREYLIST):
        return ModResult(True, True, "soft-block: greylist match")
    if len(prompt) < 5:
        return ModResult(False, False, "prompt too short")
    return ModResult(True)