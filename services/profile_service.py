from parsers import get_parser


class ProfileService:
    async def get_profile(self, site: str, username: str):
        parser = get_parser(site=site)
        if parser is None:
            raise ValueError("Unsupported chess site")
        return await parser.get_profile(username=username)

    async def get_profile_card(self, site: str, username: str):
        parser = get_parser(site=site)
        if parser is None:
            raise ValueError("Unsupported chess site")
        return await parser.get_profile_card(username=username)
