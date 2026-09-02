class PromptTemplate:
    """Prompt模板类，用于生成不同类型的Prompt"""

    def __init__(self, template: str):
        self.template = template
        self._variables = self._parse_variables(template)

    def format(self, **kwargs) -> str:
        for var in self._variables:
            if var not in kwargs:
                raise ValueError(f"缺少变量: {var}")
        return self.template.format(**kwargs)

    @property
    def variables(self) -> list[str]:
        return self._variables
    
    @staticmethod
    def _parse_variables(template: str) -> list[str]:
        """解析模板中的变量"""
        import string
        formatter = string.Formatter()

        variables = set()
        for _, field_name, _, _ in formatter.parse(template):
            if field_name:
                variables.add(field_name)
        return list(variables)